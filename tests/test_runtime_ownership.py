"""Runtime ownership tests for one shared Local Backend Workspace.

These tests prove the RFC-0018 active-binding semantics: one
``AgentRuntime`` owns exactly one Local Backend Workspace, every Session
binding borrows the same Backend instance without any BackendSession,
Workspace files are shared across bindings while cwd stays per-binding
(resume starts from the Workspace root), and Backend open failure fails
closed without creating a Runtime or attempting any fallback.
"""

import asyncio
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ScriptedModelProvider,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._backend.local import _LocalBackendWorkspace

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_runtime_owns_exactly_one_local_backend_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            backend = runtime._resources.backend
            assert isinstance(backend, _LocalBackendWorkspace)
            assert backend.root == str(tmp_path.resolve())
            assert not hasattr(runtime, "_backend")
            assert not hasattr(runtime, "_workspace")
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_each_binding_borrows_the_same_backend_workspace(tmp_path: Path) -> None:
    provider = ScriptedModelProvider(
        script=(
            (_completion(AssistantMessage.text("A")),),
            (_completion(AssistantMessage.text("B")),),
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            await _new_session(runtime)
            await _collect_turn(runtime, "A")
            first_kernel = runtime._binding.kernel
            await runtime.detach_session()

            await _new_session(runtime)
            await _collect_turn(runtime, "B")
            second_kernel = runtime._binding.kernel

            backend = runtime._resources.backend
            assert first_kernel._backend is backend
            assert second_kernel._backend is backend
            assert runtime._binding is not None
            assert runtime._binding.kernel is second_kernel
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_bindings_share_workspace_files_but_cwd_is_per_binding(
    tmp_path: Path,
) -> None:
    (tmp_path / "sub").mkdir()
    calls = (
        _exec_call("cd", "cd sub"),
        _exec_call("pwd-b", "pwd"),
        _exec_call("write", "echo hi > shared.txt"),
        _exec_call("pwd-a", "pwd"),
        _exec_call("cat", "cat shared.txt"),
    )
    provider = ScriptedModelProvider(
        script=tuple(entry for call in calls for entry in _call_script(call))
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            first = await runtime.new_session()
            await _collect_turn(runtime, "Enter sub")
            await runtime.detach_session()

            await runtime.new_session()
            await _collect_turn(runtime, "Show cwd")
            await _collect_turn(runtime, "Write shared file")
            await runtime.detach_session()

            await runtime.resume_session(first.session_id)
            await _collect_turn(runtime, "Show cwd")
            await _collect_turn(runtime, "Read shared file")
        finally:
            await runtime.close()

        assert _stdout(provider, "pwd-a") == str(tmp_path)
        assert _stdout(provider, "pwd-b") == str(tmp_path)
        assert _stdout(provider, "cat") == "hi"
        assert (tmp_path / "shared.txt").exists()

    asyncio.run(scenario())


def test_backend_open_failure_fails_runtime_open_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = workspace / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text("A\x00B=1\n", encoding="utf-8")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="must not contain NUL"):
            await AgentRuntime.open(
                workspace=workspace,
                provider=ScriptedModelProvider(script=()),
                user_interaction=_user_interaction,
                context_policy=_context_policy,
            )

    asyncio.run(scenario())


def _exec_call(call_id: str, command: str) -> ToolCall:
    return ToolCall(call_id=call_id, name="exec", arguments={"command": command})


def _call_script(
    call: ToolCall,
) -> tuple[
    tuple[ToolCallReady, ModelCompletion],
    tuple[ModelCompletion, ...],
]:
    return (
        (
            ToolCallReady(call=call),
            _completion(
                AssistantMessage(content=(call,)),
                finish_reason="tool_calls",
            ),
        ),
        (_completion(AssistantMessage.text(f"{call.call_id} done")),),
    )


def _completion(
    message: AssistantMessage,
    *,
    finish_reason: str = "stop",
) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason=finish_reason)


def _stdout(provider: ScriptedModelProvider, call_id: str) -> str:
    for request in reversed(provider.requests):
        for message in reversed(request.messages):
            if not isinstance(message, ToolResultMessage):
                continue
            for result in message.content:
                if not isinstance(result, ToolResult):
                    continue
                if result.call_id != call_id or result.error is not None:
                    continue
                output = result.output
                assert isinstance(output, dict)
                chunks = output["chunks"]
                assert isinstance(chunks, list)
                return "".join(
                    chunk["text"]
                    for chunk in chunks
                    if isinstance(chunk, dict)
                    and chunk.get("stream") == "stdout"
                    and isinstance(chunk.get("text"), str)
                ).strip()
    raise AssertionError(f"no stdout recorded for call: {call_id}")


async def _new_session(runtime: AgentRuntime) -> None:
    await runtime.new_session()


async def _collect_turn(
    runtime: AgentRuntime,
    message: str,
) -> None:
    async for _ in runtime.run_turn(UserMessage.text(message)):
        pass
