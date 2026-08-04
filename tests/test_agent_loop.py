import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from cli_agent.runtime import (
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelContextOverflowError,
    ModelEvent,
    ModelRequest,
    ScriptedModelProvider,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._agent_loop import AgentLoop
from cli_agent.runtime._environment import EnvironmentKernel

SYSTEM_MESSAGE = SystemMessage.text("Test Runtime instruction")
CONTEXT_POLICY = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_completes_a_text_only_turn(tmp_path: Path) -> None:
    user_message = UserMessage.text("Hello")
    assistant_message = AssistantMessage.text("Hi")
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="H"),
                TextDelta(text="i"),
                ModelCompletion(
                    message=assistant_message,
                    finish_reason="stop",
                ),
            ),
        )
    )
    loop = AgentLoop(
        provider,
        EnvironmentKernel(tmp_path),
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
        session_id="test-session",
    )

    events = asyncio.run(_collect_events(loop, user_message))

    assert provider.requests == (ModelRequest(messages=(SYSTEM_MESSAGE, user_message)),)
    assert events == (
        TextDelta(text="H"),
        TextDelta(text="i"),
        ModelCompletion(
            message=assistant_message,
            finish_reason="stop",
        ),
    )
    assert loop.history == (SYSTEM_MESSAGE, user_message, assistant_message)
    provider.assert_exhausted()


def test_continues_generation_after_exec_tool_result(tmp_path: Path) -> None:
    user_message = UserMessage.text("Inspect the workspace")
    call = ToolCall(
        call_id="call_1",
        name="exec",
        arguments={"command": _python_command("print('workspace inspected')")},
    )
    tool_message = AssistantMessage(
        content=(TextBlock(text="I will inspect it."), call),
    )
    final_message = AssistantMessage.text("Inspection complete.")
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="I will inspect it."),
                ToolCallReady(call=call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            ),
            (
                TextDelta(text="Inspection complete."),
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            ),
        )
    )
    kernel = EnvironmentKernel(tmp_path)
    loop = AgentLoop(
        provider,
        kernel,
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
        session_id="test-session",
    )

    events = asyncio.run(_collect_events(loop, user_message))

    assert events == (
        TextDelta(text="I will inspect it."),
        ToolCallReady(call=call),
        TextDelta(text="Inspection complete."),
        ModelCompletion(message=final_message, finish_reason="stop"),
    )
    assert len(provider.requests) == 2
    first_request, second_request = provider.requests
    assert first_request.messages == (SYSTEM_MESSAGE, user_message)
    for request in provider.requests:
        assert tuple(tool.name for tool in request.tools) == (
            "exec",
            "output",
            "kill",
        )

    assert second_request.messages[:3] == (
        SYSTEM_MESSAGE,
        user_message,
        tool_message,
    )
    result_message = second_request.messages[3]
    assert isinstance(result_message, ToolResultMessage)
    result = result_message.content[0]
    assert result.call_id == call.call_id
    assert result.error is None
    assert isinstance(result.output, dict)
    assert result.output["ok"] is True
    assert result.output["status"] == "exited"
    assert result.output["exit_code"] == 0
    chunks = result.output["chunks"]
    assert isinstance(chunks, list)
    assert "workspace inspected\n" in "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == "stdout"
    )
    assert loop.history == (
        SYSTEM_MESSAGE,
        user_message,
        tool_message,
        result_message,
        final_message,
    )
    provider.assert_exhausted()


def test_dispatches_tool_calls_in_order_and_preserves_dependencies(
    tmp_path: Path,
) -> None:
    user_message = UserMessage.text("Create and inspect a marker")
    write_call, read_call = _ordered_file_calls()
    tool_message = AssistantMessage(content=(write_call, read_call))
    final_message = AssistantMessage.text("Marker inspected.")
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=write_call),
                ToolCallReady(call=read_call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            ),
        )
    )
    loop = AgentLoop(
        provider,
        EnvironmentKernel(tmp_path),
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
        session_id="test-session",
    )

    events = asyncio.run(_collect_events(loop, user_message))

    assert events == (
        ToolCallReady(call=write_call),
        ToolCallReady(call=read_call),
        ModelCompletion(message=final_message, finish_reason="stop"),
    )
    assert len(provider.requests) == 2
    result_message = provider.requests[1].messages[-1]
    assert isinstance(result_message, ToolResultMessage)
    assert tuple(result.call_id for result in result_message.content) == (
        write_call.call_id,
        read_call.call_id,
    )
    assert _stdout(result_message.content[1]) == "written-first\n"
    assert loop.history == (
        SYSTEM_MESSAGE,
        user_message,
        tool_message,
        result_message,
        final_message,
    )
    provider.assert_exhausted()


def test_tool_call_ready_order_does_not_change_dispatch_order(
    tmp_path: Path,
) -> None:
    user_message = UserMessage.text("Create and inspect a marker")
    write_call, read_call = _ordered_file_calls()
    tool_message = AssistantMessage(content=(write_call, read_call))
    final_message = AssistantMessage.text("Marker inspected.")
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=read_call),
                TextDelta(text="Calls became ready out of order."),
                ToolCallReady(call=write_call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            ),
        )
    )
    loop = AgentLoop(
        provider,
        EnvironmentKernel(tmp_path),
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
        session_id="test-session",
    )

    events = asyncio.run(_collect_events(loop, user_message))

    assert events == (
        ToolCallReady(call=read_call),
        TextDelta(text="Calls became ready out of order."),
        ToolCallReady(call=write_call),
        ModelCompletion(message=final_message, finish_reason="stop"),
    )
    assert len(provider.requests) == 2
    result_message = provider.requests[1].messages[-1]
    assert isinstance(result_message, ToolResultMessage)
    assert tuple(result.call_id for result in result_message.content) == (
        write_call.call_id,
        read_call.call_id,
    )
    assert _stdout(result_message.content[1]) == "written-first\n"
    provider.assert_exhausted()


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _ordered_file_calls() -> tuple[ToolCall, ToolCall]:
    write_call = ToolCall(
        call_id="write_marker",
        name="exec",
        arguments={
            "command": _python_command(
                "from pathlib import Path; "
                "Path('order.txt').write_text('written-first')"
            )
        },
    )
    read_call = ToolCall(
        call_id="read_marker",
        name="exec",
        arguments={
            "command": _python_command(
                "from pathlib import Path; print(Path('order.txt').read_text())"
            )
        },
    )
    return write_call, read_call


def _stdout(result: ToolResult) -> str:
    output = result.output
    assert isinstance(output, dict)
    chunks = output["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == "stdout"
    )


def test_recovers_from_provider_context_overflow_and_retries_once(
    tmp_path: Path,
) -> None:
    assistant_message = AssistantMessage.text("Recovered")
    provider = _OverflowThenSuccessProvider(
        success_events=(TextDelta(text="Recovered"), _completion(assistant_message)),
    )
    received: list[object] = []
    loop = AgentLoop(
        provider,
        EnvironmentKernel(tmp_path),
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
        session_id="overflow-session",
        on_diagnostic=received.append,
    )

    events = asyncio.run(_collect_events(loop, UserMessage.text("Hello")))

    assert events == (
        TextDelta(text="Recovered"),
        ModelCompletion(message=assistant_message, finish_reason="stop"),
    )
    assert len(provider.requests) == 2
    assert provider.requests[0] == provider.requests[1]
    assert loop.history == (
        SYSTEM_MESSAGE,
        UserMessage.text("Hello"),
        assistant_message,
    )
    assert len(received) == 1
    diagnostic = received[0]
    assert diagnostic.kind == "context.overflow_recovery"
    assert diagnostic.detail["session_id"] == "overflow-session"
    assert diagnostic.detail["revision"] == 1


def test_raises_stable_error_after_second_provider_overflow(tmp_path: Path) -> None:
    provider = _OverflowThenSuccessProvider(
        success_events=(),
        fail_twice=True,
    )
    loop = AgentLoop(
        provider,
        EnvironmentKernel(tmp_path),
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
        session_id="overflow-session",
    )

    with pytest.raises(ModelContextOverflowError):
        asyncio.run(_collect_events(loop, UserMessage.text("Hello")))

    assert len(provider.requests) == 2


def _completion(message: AssistantMessage) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop")


class _OverflowThenSuccessProvider:
    def __init__(
        self,
        *,
        success_events: tuple[ModelEvent, ...],
        fail_twice: bool = False,
    ) -> None:
        self._success_events = success_events
        self._fail_twice = fail_twice
        self._failures_left = 2 if fail_twice else 1
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    async def generate(self, request: ModelRequest):
        self._requests.append(request)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise ModelContextOverflowError("provider context overflow")
        for event in self._success_events:
            yield event


async def _collect_events(
    loop: AgentLoop,
    user_message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in loop.run(user_message)])
