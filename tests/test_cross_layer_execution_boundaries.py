"""Cross-layer contract tests for the AST routing and policy boundaries.

These tests pin the single main execution path through the public
``AgentRuntime`` surface: parse, route, optional Policy, optional
UserInteraction, admission, and execution. They prove that parse
failures and every fail-closed branch create no Execution resources,
and that ``policy=None`` and configured Policy paths stay end-to-end
consistent.
"""

import asyncio
import shlex
import sys
from pathlib import Path

from host_fakes import _environment_kernel
from interaction_fakes import _BlockingInteraction, _ScriptedInteraction
from policy_fakes import _AllowAllPolicy, _AskExecutablePolicy, _DenyExecutablePolicy
from workspace_fakes import _kernel_workspace

from cli_agent.presets import open_default_runtime
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_unsupported_valid_syntax_falls_back_to_shell(tmp_path: Path) -> None:
    async def scenario() -> None:
        proof = tmp_path / "unsupported-syntax-proof"
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="unsupported-syntax",
                    name="exec",
                    arguments={"command": f"if true; then {_touch_command(proof)}; fi"},
                )
            )

            assert result.error is None
            assert _output(result)["status"] == "exited"
            assert proof.exists()
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_policy_none_runs_end_to_end_without_interaction(tmp_path: Path) -> None:
    interaction = _ScriptedInteraction("allow_once")
    proof = tmp_path / "policy-none-proof"
    provider = _provider_script(
        ToolCall(
            call_id="policy_none_exec",
            name="exec",
            arguments={"command": _touch_command(proof)},
        ),
        "Runs without policy.",
    )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=provider,
            interaction=interaction,
            context_policy=_context_policy,
        )
        try:
            await _collect_turn(runtime, "Run it")
        finally:
            await runtime.close()

    asyncio.run(scenario())

    assert proof.exists()
    assert interaction.questions == []
    assert _last_tool_error(provider) is None


def test_configured_policy_paths_end_to_end(tmp_path: Path) -> None:
    denied_proof = tmp_path / "denied-proof"
    allowed_proof = tmp_path / "allowed-proof"

    async def scenario() -> None:
        deny_runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=_provider_script(
                ToolCall(
                    call_id="denied_exec",
                    name="exec",
                    arguments={"command": f"touch {denied_proof}"},
                ),
                "Denied.",
            ),
            interaction=_ScriptedInteraction("allow_once"),
            policy=_DenyExecutablePolicy(
                frozenset({"touch"}),
                reason="touch is denied by policy",
            ),
            context_policy=_context_policy,
        )
        try:
            await _collect_turn(deny_runtime, "Deny it")
        finally:
            await deny_runtime.close()

        allow_runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=_provider_script(
                ToolCall(
                    call_id="allowed_exec",
                    name="exec",
                    arguments={"command": f"touch {allowed_proof}"},
                ),
                "Allowed.",
            ),
            interaction=_ScriptedInteraction("allow_once"),
            policy=_AllowAllPolicy(),
            context_policy=_context_policy,
        )
        try:
            await _collect_turn(allow_runtime, "Allow it")
        finally:
            await allow_runtime.close()

    asyncio.run(scenario())

    assert not denied_proof.exists()
    assert allowed_proof.exists()


def test_ask_interaction_allow_once_and_deny_end_to_end(tmp_path: Path) -> None:
    allowed_proof = tmp_path / "ask-allowed-proof"
    denied_proof = tmp_path / "ask-denied-proof"
    allow_interaction = _ScriptedInteraction("allow_once")
    deny_interaction = _ScriptedInteraction("deny")

    async def scenario() -> None:
        allow_runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=_provider_script(
                ToolCall(
                    call_id="ask_allowed",
                    name="exec",
                    arguments={"command": _touch_command(allowed_proof)},
                ),
                "Allowed once.",
            ),
            interaction=allow_interaction,
            policy=_ask_python_policy(),
            context_policy=_context_policy,
        )
        try:
            await _collect_turn(allow_runtime, "Allow it")
        finally:
            await allow_runtime.close()

        deny_runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=_provider_script(
                ToolCall(
                    call_id="ask_denied",
                    name="exec",
                    arguments={"command": _touch_command(denied_proof)},
                ),
                "Denied.",
            ),
            interaction=deny_interaction,
            policy=_ask_python_policy(),
            context_policy=_context_policy,
        )
        try:
            await _collect_turn(deny_runtime, "Deny it")
        finally:
            await deny_runtime.close()

    asyncio.run(scenario())

    assert allowed_proof.exists()
    assert not denied_proof.exists()
    assert len(allow_interaction.questions) == 1
    assert "python requires approval" in allow_interaction.questions[0].prompt
    assert "allow_once" in {
        option.value for option in allow_interaction.questions[0].options
    }
    assert len(deny_interaction.questions) == 1


def test_parse_failure_through_public_runtime_creates_no_execution(
    tmp_path: Path,
) -> None:
    provider = _provider_script(
        ToolCall(
            call_id="malformed_exec",
            name="exec",
            arguments={"command": "echo 'unterminated"},
        ),
        "Invalid command reported.",
    )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=provider,
            interaction=_ScriptedInteraction("allow_once"),
            policy=_AskExecutablePolicy(
                frozenset({"echo"}),
                rule_id="test.ask-echo",
                reason="echo requires approval",
            ),
            context_policy=_context_policy,
        )
        try:
            await _collect_turn(runtime, "Run it")
        finally:
            await runtime.close()

    asyncio.run(scenario())

    error = _last_tool_error(provider)
    assert error is not None
    assert error["code"] == "invalid_argument"
    assert error["message"] == "invalid shell command"


def test_runtime_close_cancels_pending_ask_without_closing_interaction(
    tmp_path: Path,
) -> None:
    interaction = _BlockingInteraction()
    provider = _provider_script(
        ToolCall(
            call_id="pending_ask",
            name="exec",
            arguments={"command": _touch_command(tmp_path / "pending-proof")},
        ),
        "Finished.",
    )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=provider,
            interaction=interaction,
            policy=_ask_python_policy(),
            context_policy=_context_policy,
        )
        turn = asyncio.create_task(
            _collect_turn(runtime, "Ask for approval")
        )
        await interaction.entered.wait()
        await runtime.close()

        await asyncio.wait_for(turn, timeout=0.5)
        assert interaction.cancelled.is_set()
        assert interaction.closed is False
        assert not (tmp_path / "pending-proof").exists()

    asyncio.run(scenario())


def test_session_remains_usable_after_denial(tmp_path: Path) -> None:
    denied_proof = tmp_path / "denied-proof"
    later_proof = tmp_path / "later-proof"
    denied_call = ToolCall(
        call_id="denied_exec",
        name="exec",
        arguments={"command": f"touch {denied_proof}"},
    )
    later_call = ToolCall(
        call_id="later_exec",
        name="exec",
        arguments={"command": _touch_command(later_proof)},
    )
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=denied_call),
                ModelCompletion(
                    message=AssistantMessage(content=(denied_call,)),
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=AssistantMessage.text("Denied."),
                    finish_reason="stop",
                ),
            ),
            (
                ToolCallReady(call=later_call),
                ModelCompletion(
                    message=AssistantMessage(content=(later_call,)),
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                ),
            ),
        )
    )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=provider,
            interaction=_ScriptedInteraction("allow_once"),
            policy=_DenyExecutablePolicy(
                frozenset({"touch"}),
                reason="touch is denied by policy",
            ),
            context_policy=_context_policy,
        )
        try:
            await _collect_turn(runtime, "Deny it")
            await _collect_turn(runtime, "Run later work")
        finally:
            await runtime.close()

    asyncio.run(scenario())

    assert not denied_proof.exists()
    assert later_proof.exists()


def _ask_python_policy() -> _AskExecutablePolicy:
    return _AskExecutablePolicy(
        frozenset({Path(sys.executable).name}),
        rule_id="test.ask-python",
        reason="python requires approval",
    )


def _provider_script(
    call: ToolCall,
    final_text: str,
) -> ScriptedModelProvider:
    return ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=call),
                ModelCompletion(
                    message=AssistantMessage(content=(call,)),
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=AssistantMessage.text(final_text),
                    finish_reason="stop",
                ),
            ),
        )
    )


def _last_tool_error(
    provider: ScriptedModelProvider,
) -> dict[str, object] | None:
    for request in reversed(provider.requests):
        for message in reversed(request.messages):
            if isinstance(message, ToolResultMessage):
                result = message.content[0]
                return result.error
    return None


async def _collect_turn(
    runtime: AgentRuntime,
    text: str,
) -> tuple[ModelEvent, ...]:
    if runtime._binding is None:
        await runtime.new_session()
    return tuple(
        [
            event
            async for event in runtime.run_turn(UserMessage.text(text))
        ]
    )


def _touch_command(path: Path) -> str:
    source = f"from pathlib import Path; Path({str(path)!r}).touch()"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output
