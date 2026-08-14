"""End-to-end Context compaction trajectories through the public Runtime.

These tests drive the full Runtime order - append, prepare, generate, observe,
dispatch, append - with a small Context Window, the deterministic token meter,
and scripted providers, and pin the four-tier boundaries, Session isolation,
and overflow recovery without any live Provider.
"""

import asyncio
import shlex
import sys
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelContextOverflowError,
    ModelEvent,
    ModelRequest,
    ScriptedModelProvider,
    SystemMessage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._context.tokens import estimate_request_tokens
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

_user_interaction = _ScriptedInteraction("deny")

_LARGE_BUDGET = ContextPolicy(
    context_window_tokens=205_000,
    output_reserve_tokens=5_000,
    safety_margin_tokens=0,
    minimum_reclaim_tokens=1,
)
_STANDARD_BUDGET = ContextPolicy(
    context_window_tokens=128_000,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
    minimum_reclaim_tokens=1,
)


def _print_command(char_count: int) -> str:
    source = f"print('x' * {char_count})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _completion(message: AssistantMessage) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop")


def _tool_call(call_id: str, command: str) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="exec",
        arguments={"command": command, "wait_ms": 20_000},
    )


def _exec_step(call: ToolCall) -> tuple[ModelEvent, ...]:
    return (_completion(AssistantMessage(content=(call,))),)


def _plain_step(text: str) -> tuple[ModelEvent, ...]:
    return (_completion(AssistantMessage.text(text)),)


def _result_state(result: ToolResult) -> str | None:
    output = result.output
    if isinstance(output, dict):
        reclaimed = output.get("reclaimed")
        if isinstance(reclaimed, dict):
            return reclaimed["state"]
    return None


def _tool_results(request: ModelRequest) -> list[ToolResultMessage]:
    return [
        message
        for message in request.messages
        if isinstance(message, ToolResultMessage)
    ]


def _exec_ids(request: ModelRequest) -> set[str]:
    ids: set[str] = set()
    for message in _tool_results(request):
        for result in message.content:
            output = result.output
            if isinstance(output, dict) and isinstance(output.get("exec_id"), str):
                ids.add(output["exec_id"])
    return ids


async def _run_turn(
    runtime: AgentRuntime,
    session_id: str,
    text: str,
    *,
    provider=None,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(
                session_id,
                UserMessage.text(text),
                provider=provider,
            )
        ]
    )


def test_tier0_and_tier1_snip_trajectory(
    tmp_path: Path,
) -> None:
    old_call = _tool_call("call_old", _print_command(300_000))
    recent_call = _tool_call("call_recent", _print_command(50_000))
    provider = ScriptedModelProvider(
        script=(
            _exec_step(old_call),
            _plain_step("old turn done"),
            _exec_step(recent_call),
            _plain_step("recent turn done"),
            _plain_step("final answer"),
        )
    )
    received: list[RuntimeDiagnostic] = []
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, received))
    try:
        asyncio.run(_run_turn(runtime, "session", "Inspect the old workspace"))
        asyncio.run(_run_turn(runtime, "session", "Inspect the recent marker"))
        asyncio.run(_run_turn(runtime, "session", "Summarize the state"))

        requests = provider.requests
        assert len(requests) == 5
        final_request = requests[4]
        results = _tool_results(final_request)
        assert len(results) == 2
        old_result, recent_result = (message.content[0] for message in results)
        assert _result_state(old_result) == "snipped"
        assert _result_state(recent_result) is None
        assert old_result.call_id == old_call.call_id
        assert recent_result.call_id == recent_call.call_id
        assert final_request.messages[0] is requests[0].messages[0]
        kinds = [diagnostic.kind for diagnostic in received]
        assert "context.snipped" in kinds
        assert "context.pruned" not in kinds
        provider.assert_exhausted()
    finally:
        asyncio.run(runtime.close())


def test_tier2_prune_trajectory(
    tmp_path: Path,
) -> None:
    old_call = _tool_call("call_old", _print_command(44_000))
    recent_call = _tool_call("call_recent", _print_command(400_000))
    provider = ScriptedModelProvider(
        script=(
            _exec_step(old_call),
            _plain_step("old turn done"),
            _exec_step(recent_call),
            _plain_step("recent turn done"),
            _plain_step("final answer"),
        )
    )
    received: list[RuntimeDiagnostic] = []
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, received))
    try:
        asyncio.run(_run_turn(runtime, "session", "Inspect the old workspace"))
        asyncio.run(_run_turn(runtime, "session", "Inspect the recent marker"))
        asyncio.run(_run_turn(runtime, "session", "Summarize the state"))

        requests = provider.requests
        final_request = requests[4]
        old_result, recent_result = (
            message.content[0] for message in _tool_results(final_request)
        )
        assert _result_state(old_result) == "pruned"
        assert _result_state(recent_result) is None
        kinds = [diagnostic.kind for diagnostic in received]
        assert "context.snipped" in kinds
        assert "context.pruned" in kinds
        assert "context.summarized" not in kinds
        provider.assert_exhausted()
    finally:
        asyncio.run(runtime.close())


def test_tier3_summarize_trajectory(
    tmp_path: Path,
) -> None:
    summary_text = (
        "## Progress\ninspected the workspace\n"
        "## Files\nreport.txt located\n"
        "## Todo\nverify the fix\n"
        "## Context\nuser wants concise answers"
    )
    provider = ScriptedModelProvider(
        script=(
            _plain_step("x" * 480_000),
            _plain_step("x" * 80_000),
            (
                ModelCompletion(
                    message=AssistantMessage.text(summary_text), finish_reason="stop"
                ),
            ),
            _plain_step("final answer"),
        )
    )
    received: list[RuntimeDiagnostic] = []
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, received))
    try:
        asyncio.run(_run_turn(runtime, "session", "First old discussion"))
        asyncio.run(_run_turn(runtime, "session", "Second recent discussion"))
        events = asyncio.run(_run_turn(runtime, "session", "Wrap up"))

        assert events[-1] == ModelCompletion(
            message=AssistantMessage.text("final answer"),
            finish_reason="stop",
        )
        requests = provider.requests
        assert len(requests) == 4
        summary_request = requests[2]
        assert summary_request.tools == ()
        final_request = requests[3]
        system_message = final_request.messages[0]
        assert isinstance(system_message, SystemMessage)
        summary_projection = final_request.messages[1]
        assert isinstance(summary_projection, AssistantMessage)
        projected_text = summary_projection.content[0].text
        assert projected_text.startswith("<session-summary>")
        for section in ("## Progress", "## Files", "## Todo", "## Context"):
            assert section in projected_text
        assert not any(
            isinstance(message, SystemMessage) for message in final_request.messages[1:]
        )
        assert final_request.messages[2] == UserMessage.text("Second recent discussion")
        kinds = [diagnostic.kind for diagnostic in received]
        assert "context.summarized" in kinds
        provider.assert_exhausted()
    finally:
        asyncio.run(runtime.close())


def test_initial_pressure_above_summarize_but_snip_suffices(
    tmp_path: Path,
) -> None:
    old_call = _tool_call("call_old", _print_command(300_000))
    recent_call = _tool_call("call_recent", _print_command(460_000))
    provider = ScriptedModelProvider(
        script=(
            _exec_step(old_call),
            _plain_step("old turn done"),
            _exec_step(recent_call),
            _plain_step("recent turn done"),
            _plain_step("final answer"),
        )
    )
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _LARGE_BUDGET, None))
    try:
        asyncio.run(_run_turn(runtime, "session", "Inspect the old workspace"))
        asyncio.run(_run_turn(runtime, "session", "Inspect the recent marker"))
        asyncio.run(_run_turn(runtime, "session", "Summarize the state"))

        assert len(provider.requests) == 5
        final_request = provider.requests[4]
        old_result, recent_result = (
            message.content[0] for message in _tool_results(final_request)
        )
        assert _result_state(old_result) == "snipped"
        assert _result_state(recent_result) is None
        provider.assert_exhausted()
    finally:
        asyncio.run(runtime.close())


def test_overflow_recovers_and_retries_once_without_repeating_tools(
    tmp_path: Path,
) -> None:
    marker_source = (
        "from pathlib import Path; "
        "p = Path('marker.txt'); "
        "assert not p.exists(); "
        "p.write_text('x')"
    )
    marker_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(marker_source)}"
    call = _tool_call("call_marker", marker_command)
    provider = _OverflowThenSuccessProvider(
        script=(
            (_completion(AssistantMessage(content=(call,))),),
            _plain_step("marker created once"),
        ),
    )
    received: list[RuntimeDiagnostic] = []
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, received))
    try:
        events = asyncio.run(_run_turn(runtime, "session", "Create a marker once"))

        assert len(provider.requests) == 3
        assert [diagnostic.kind for diagnostic in received] == [
            "context.overflow_recovery"
        ]
        marker = tmp_path / "marker.txt"
        assert marker.read_text() == "x"
        assert events == (
            ModelCompletion(
                message=AssistantMessage.text("marker created once"),
                finish_reason="stop",
            ),
        )
    finally:
        asyncio.run(runtime.close())


def test_second_overflow_propagates_and_session_stays_closable(
    tmp_path: Path,
) -> None:
    provider = _OverflowThenSuccessProvider(
        script=(),
        fail_twice=True,
    )
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, None))
    try:
        with pytest.raises(ModelContextOverflowError):
            asyncio.run(_run_turn(runtime, "session", "Hello"))
        assert len(provider.requests) == 2
    finally:
        asyncio.run(runtime.close())
        assert runtime.closed


def test_cross_session_compaction_is_isolated(
    tmp_path: Path,
) -> None:
    old_call = _tool_call("call_old", _print_command(300_000))
    recent_call = _tool_call("call_recent", _print_command(50_000))
    provider_a = ScriptedModelProvider(
        script=(
            _exec_step(old_call),
            _plain_step("old turn done"),
            _exec_step(recent_call),
            _plain_step("recent turn done"),
            _plain_step("final answer"),
        )
    )
    provider_b = ScriptedModelProvider(script=(_plain_step("plain session answer"),))
    runtime = asyncio.run(_open_runtime(tmp_path, provider_a, _STANDARD_BUDGET, None))
    try:
        asyncio.run(_run_turn(runtime, "session-a", "Inspect the old workspace"))
        asyncio.run(_run_turn(runtime, "session-a", "Inspect the recent marker"))
        asyncio.run(_run_turn(runtime, "session-a", "Summarize the state"))
        asyncio.run(_run_turn(runtime, "session-b", "Hello", provider=provider_b))

        final_request_a = provider_a.requests[4]
        old_result, _recent = (
            message.content[0] for message in _tool_results(final_request_a)
        )
        assert _result_state(old_result) == "snipped"
        assert provider_b.requests == (
            ModelRequest(
                messages=(provider_b.requests[0].messages[0], UserMessage.text("Hello"))
            ),
        )
        provider_a.assert_exhausted()
        provider_b.assert_exhausted()
    finally:
        asyncio.run(runtime.close())


def test_close_session_releases_context_and_new_id_is_fresh(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider(
        script=(
            _plain_step("first answer"),
            _plain_step("fresh answer"),
        )
    )
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, None))
    try:
        asyncio.run(_run_turn(runtime, "session", "First turn"))
        asyncio.run(runtime.close_session("session"))
        asyncio.run(_run_turn(runtime, "fresh-session", "Second turn"))

        first_system = provider.requests[0].messages[0]
        second_system = provider.requests[1].messages[0]
        assert isinstance(first_system, SystemMessage)
        assert isinstance(second_system, SystemMessage)
        assert second_system is not first_system
        assert provider.requests[1].messages == (
            second_system,
            UserMessage.text("Second turn"),
        )
        provider.assert_exhausted()
    finally:
        asyncio.run(runtime.close())


async def _open_runtime(
    tmp_path: Path,
    provider,
    context_policy: ContextPolicy,
    received: list[RuntimeDiagnostic] | None,
) -> AgentRuntime:
    return await AgentRuntime.open(
        workspace=tmp_path,
        provider=provider,
        user_interaction=_user_interaction,
        context_policy=context_policy,
        on_diagnostic=received.append if received is not None else None,
    )


class _OverflowThenSuccessProvider:
    def __init__(
        self,
        *,
        script: tuple[tuple[ModelEvent, ...], ...],
        fail_twice: bool = False,
    ) -> None:
        self._script = script
        self._failures_left = 2 if fail_twice else 1
        self._stream_index = 0
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    async def generate(self, request: ModelRequest):
        self._requests.append(request)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise ModelContextOverflowError("provider context overflow")
        if self._stream_index >= len(self._script):
            raise RuntimeError(
                "provider received more model requests than scripted: "
                f"expected {len(self._script)}"
            )
        events = self._script[self._stream_index]
        self._stream_index += 1
        for event in events:
            yield event


def _python(command_source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(command_source)}"


class _StreamQueueProvider:
    def __init__(self) -> None:
        self._streams: list[tuple[ModelEvent, ...]] = []
        self._index = 0
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    def add_stream(self, events: tuple[ModelEvent, ...]) -> None:
        self._streams.append(events)

    async def generate(self, request: ModelRequest):
        self._requests.append(request)
        if self._index >= len(self._streams):
            raise RuntimeError(
                "provider received more model requests than scripted: "
                f"expected {len(self._streams)}"
            )
        events = self._streams[self._index]
        self._index += 1
        for event in events:
            yield event


async def run_file_exploration_trajectory(
    runtime: AgentRuntime,
    provider: _StreamQueueProvider,
) -> dict[str, object]:
    """Explore a Workspace with repeated partial file reads and report facts."""

    list_call = _tool_call(
        "call_list",
        _python("import os; print('\\n'.join(sorted(os.listdir('.'))))"),
    )
    read_call = _tool_call(
        "call_read",
        _python("print(open('notes.txt').read()[:200])"),
    )
    provider.add_stream(_exec_step(list_call))
    provider.add_stream(_plain_step("Listed the workspace files."))
    provider.add_stream(_exec_step(read_call))
    provider.add_stream(_plain_step("Read the notes file."))
    provider.add_stream(
        _plain_step("Found notes.txt; it contains the marker value 42.")
    )
    await _run_turn(runtime, "eval-a", "Explore the workspace")
    await _run_turn(runtime, "eval-a", "Read the notes file")
    events = await _run_turn(runtime, "eval-a", "Report what you found")
    return {
        "requests": len(provider.requests),
        "input_tokens": [
            estimate_request_tokens(request) for request in provider.requests
        ],
        "final_text": events[-1].message.content[0].text,
    }


async def run_output_polling_trajectory(
    runtime: AgentRuntime,
    provider: _StreamQueueProvider,
) -> dict[str, object]:
    """Start a long execution and poll its retained output with `output`."""

    start_call = ToolCall(
        call_id="call_start",
        name="exec",
        arguments={
            "command": _python(
                "import time; [print(f'line{i}') for i in range(20)]; time.sleep(0.2)"
            ),
            "wait_ms": 0,
        },
    )
    provider.add_stream(_exec_step(start_call))
    provider.add_stream(_plain_step("Started; polling output now."))
    await _run_turn(runtime, "eval-b", "Start the long command")
    first = provider.requests[1]
    start_result = next(
        message.content[0] for message in _tool_results(first) if message.content
    )
    output = start_result.output
    assert isinstance(output, dict) and isinstance(output.get("exec_id"), str)
    poll_call = ToolCall(
        call_id="call_poll",
        name="output",
        arguments={"exec_id": output["exec_id"], "cursor": 1_000, "wait_ms": 5_000},
    )
    provider.add_stream(_exec_step(poll_call))
    provider.add_stream(_plain_step("Output complete: 20 lines."))
    events = await _run_turn(runtime, "eval-b", "Poll until the command finishes")
    return {
        "requests": len(provider.requests),
        "exec_id": output["exec_id"],
        "final_text": events[-1].message.content[0].text,
    }


async def run_edit_test_fix_verify_trajectory(
    runtime: AgentRuntime,
    provider: _StreamQueueProvider,
) -> dict[str, object]:
    """Edit a file, run a failing test, fix it, and verify the fix."""

    write_call = _tool_call(
        "call_write",
        _python("open('app.py', 'w').write('def add(a, b):\\n    return a - b\\n')"),
    )
    test_call = _tool_call(
        "call_test",
        _python("from app import add; assert add(2, 2) == 4"),
    )
    fix_call = _tool_call(
        "call_fix",
        _python(
            "open('app.py', 'w').write('def add(a, b):\\n    return a + b  # fixed\\n')"
        ),
    )
    verify_call = _tool_call(
        "call_verify",
        _python("from app import add; assert add(2, 2) == 4; print('verified')"),
    )
    provider.add_stream(_exec_step(write_call))
    provider.add_stream(_plain_step("Wrote the first version."))
    provider.add_stream(_exec_step(test_call))
    provider.add_stream(_plain_step("The test failed; fixing now."))
    provider.add_stream(_exec_step(fix_call))
    provider.add_stream(_plain_step("Fixed the function."))
    provider.add_stream(_exec_step(verify_call))
    provider.add_stream(_plain_step("Verified the fix."))
    provider.add_stream(_plain_step("Fixed and verified: add(2, 2) returns 4."))
    await _run_turn(runtime, "eval-c", "Write a first version of add")
    await _run_turn(runtime, "eval-c", "Run the add test")
    await _run_turn(runtime, "eval-c", "Fix the function")
    await _run_turn(runtime, "eval-c", "Verify the fix")
    events = await _run_turn(runtime, "eval-c", "Report the outcome")
    return {
        "requests": len(provider.requests),
        "test_exit_code": _exit_code(provider.requests[3]),
        "verify_exit_code": _exit_code(provider.requests[7]),
        "final_text": events[-1].message.content[0].text,
    }


def _exit_code(request: ModelRequest) -> int | None:
    messages = _tool_results(request)
    if not messages:
        return None
    result = messages[-1].content[-1]
    output = result.output
    if isinstance(output, dict):
        exit_code = output.get("exit_code")
        if isinstance(exit_code, int):
            return exit_code
    return None


def test_file_exploration_fixture_preserves_facts(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text(
        "the marker value is 42\nand more notes\n",
        encoding="utf-8",
    )
    provider = _StreamQueueProvider()
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, None))
    try:
        metrics = asyncio.run(run_file_exploration_trajectory(runtime, provider))
        assert metrics["requests"] == 5
        assert "42" in metrics["final_text"]
        assert metrics["final_text"].startswith("Found notes.txt")
        assert metrics["input_tokens"][0] < metrics["input_tokens"][1]
    finally:
        asyncio.run(runtime.close())


def test_output_polling_fixture_reuses_execution_state(
    tmp_path: Path,
) -> None:
    provider = _StreamQueueProvider()
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, None))
    try:
        metrics = asyncio.run(run_output_polling_trajectory(runtime, provider))
        assert metrics["requests"] == 4
        assert "20 lines" in metrics["final_text"]
        assert metrics["exec_id"]
    finally:
        asyncio.run(runtime.close())


def test_edit_test_fix_verify_fixture_records_round_trip(
    tmp_path: Path,
) -> None:
    provider = _StreamQueueProvider()
    runtime = asyncio.run(_open_runtime(tmp_path, provider, _STANDARD_BUDGET, None))
    try:
        metrics = asyncio.run(run_edit_test_fix_verify_trajectory(runtime, provider))
        assert metrics["requests"] == 9
        assert metrics["test_exit_code"] == 1
        assert metrics["verify_exit_code"] == 0
        assert "returns 4" in metrics["final_text"]
        assert (tmp_path / "app.py").read_text() == (
            "def add(a, b):\n    return a + b  # fixed\n"
        )
    finally:
        asyncio.run(runtime.close())
