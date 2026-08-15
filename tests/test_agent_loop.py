import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from cli_agent.errors.context import ContextExhaustedError
from cli_agent.runtime import (
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ModelMessage,
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
from cli_agent.runtime._agent_loop import (
    PRINT_HISTORY_ENV,
    AgentLoop,
    _render_history,
)
from cli_agent.runtime._context.engine import _ContextEngine
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime.model import ModelContextOverflowSignal

SYSTEM_MESSAGE = SystemMessage.text("Test Runtime instruction")
CONTEXT_POLICY = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def _new_loop(
    provider: object,
    kernel: object,
    *,
    session_id: str = "test-session",
    on_diagnostic: object = None,
) -> AgentLoop:
    engine = _ContextEngine(
        session_id=session_id,
        context_policy=CONTEXT_POLICY,
        provider=provider,  # type: ignore[arg-type]
        on_diagnostic=on_diagnostic,  # type: ignore[arg-type]
    )
    engine.hydrate(system_message=SYSTEM_MESSAGE, snapshot=None, journal=(), revision=0)

    def commit(message: ModelMessage) -> int:
        del message
        return engine.revision + 1

    return AgentLoop(
        provider,  # type: ignore[arg-type]
        kernel,  # type: ignore[arg-type]
        context=engine,
        commit=commit,
        on_diagnostic=on_diagnostic,  # type: ignore[arg-type]
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
    loop = _new_loop(provider, EnvironmentKernel(tmp_path))

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
    loop = _new_loop(provider, EnvironmentKernel(tmp_path))

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
    loop = _new_loop(provider, EnvironmentKernel(tmp_path))

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
    loop = _new_loop(provider, EnvironmentKernel(tmp_path))

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
    loop = _new_loop(
        provider,
        EnvironmentKernel(tmp_path),
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
    loop = _new_loop(
        provider,
        EnvironmentKernel(tmp_path),
        session_id="overflow-session",
    )

    with pytest.raises(ContextExhaustedError) as raised:
        asyncio.run(_collect_events(loop, UserMessage.text("Hello")))

    assert raised.value.code == "context_exhausted"
    assert raised.value.details["session_id"] == "overflow-session"

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
            raise ModelContextOverflowSignal("provider context overflow")
        for event in self._success_events:
            yield event


class _RecordingKernel:
    """Kernel stub recording hook and dispatch calls in arrival order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reconcile_library(self) -> None:
        self.calls.append("reconcile")

    async def dispatch_batch(
        self,
        calls: tuple[ToolCall, ...],
    ) -> tuple[ToolResult, ...]:
        self.calls.append("dispatch")
        return tuple(ToolResult(call_id=call.call_id, output={}) for call in calls)


def test_reconcile_runs_before_every_model_request() -> None:
    user_message = UserMessage.text("Inspect")
    call = ToolCall(call_id="call_1", name="exec", arguments={"command": "true"})
    tool_message = AssistantMessage(content=(call,))
    final_message = AssistantMessage.text("Done.")
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=call),
                ModelCompletion(message=tool_message, finish_reason="tool_calls"),
            ),
            (ModelCompletion(message=final_message, finish_reason="stop"),),
        )
    )
    kernel = _RecordingKernel()
    loop = _new_loop(provider, kernel)

    asyncio.run(_collect_events(loop, user_message))

    assert kernel.calls == ["reconcile", "dispatch", "reconcile"]
    assert len(provider.requests) == 2


async def _collect_events(
    loop: AgentLoop,
    user_message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in loop.run(user_message)])


def _two_step_loop(kernel: EnvironmentKernel | _RecordingKernel) -> AgentLoop:
    call = ToolCall(call_id="call_1", name="exec", arguments={"command": "true"})
    provider = ScriptedModelProvider(
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
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    return _new_loop(provider, kernel)


def test_history_print_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(PRINT_HISTORY_ENV, raising=False)
    loop = _two_step_loop(_RecordingKernel())

    asyncio.run(_collect_events(loop, UserMessage.text("Inspect")))

    assert "HISTORY (" not in capsys.readouterr().err


def test_history_print_requires_exact_value_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for value in ("true", "yes", "on", "2", ""):
        monkeypatch.setenv(PRINT_HISTORY_ENV, value)
        loop = _two_step_loop(_RecordingKernel())

        asyncio.run(_collect_events(loop, UserMessage.text("Inspect")))

        assert "HISTORY (" not in capsys.readouterr().err


def test_history_print_dumps_readable_history_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(PRINT_HISTORY_ENV, "1")
    loop = _two_step_loop(_RecordingKernel())

    asyncio.run(_collect_events(loop, UserMessage.text("Inspect")))

    err = capsys.readouterr().err
    assert err.count("HISTORY (") == 2
    assert "[1] SYSTEM" in err
    assert "[2] USER" in err
    assert "-> exec [call_1]" in err
    assert "<- call_1" in err


def test_render_history_formats_all_message_kinds() -> None:
    rendered = _render_history(
        (
            SystemMessage.text("instruction"),
            UserMessage.text("hi"),
            AssistantMessage(
                content=(
                    TextBlock(text="ok"),
                    ToolCall(
                        call_id="c1",
                        name="exec",
                        arguments={"command": "ls"},
                    ),
                )
            ),
            ToolResultMessage(content=(ToolResult(call_id="c1", output={"ok": True}),)),
            AssistantMessage.text("done"),
        )
    )

    assert rendered.startswith("=" * 60)
    assert "HISTORY (5 messages)" in rendered
    assert "[3] ASSISTANT" in rendered
    assert "-> exec [c1]" in rendered
    assert "[4] TOOL RESULT" in rendered
    assert "<- c1" in rendered
    assert "[5] ASSISTANT" in rendered
    assert rendered.endswith("=" * 60)
