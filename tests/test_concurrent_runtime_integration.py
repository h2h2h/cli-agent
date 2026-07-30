import asyncio
import shlex
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    ScriptedModelProvider,
    SystemMessage,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._environment.execution import _ExecutionState


class _CoordinatedProvider:
    def __init__(
        self,
        role: str,
        first_calls: tuple[ToolCall, ...],
    ) -> None:
        self.role = role
        self.first_calls = first_calls
        self.peer: _CoordinatedProvider | None = None
        self.initial_results_ready = asyncio.Event()
        self.ready_for_close = asyncio.Event()
        self.finish_allowed = asyncio.Event()
        self.initial_results: tuple[ToolResult, ...] | None = None
        self.foreign_calls: tuple[ToolCall, ...] = ()
        self.foreign_results: tuple[ToolResult, ...] | None = None
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    async def generate(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelEvent]:
        stage = len(self._requests)
        self._requests.append(request)
        if stage == 0:
            async for event in _tool_completion(self.first_calls):
                yield event
            return

        if stage == 1:
            self.initial_results = _last_tool_results(request)
            self.initial_results_ready.set()
            if self.role == "session-b":
                await self.finish_allowed.wait()
                async for event in _text_completion("Session B completed."):
                    yield event
                return

            if self.peer is None:
                raise AssertionError("Session A provider has no peer")
            await self.peer.initial_results_ready.wait()
            if self.peer.initial_results is None:
                raise AssertionError("Session B results were not captured")
            foreign_exec_id = str(
                _result_output(self.peer.initial_results[0])["exec_id"]
            )
            self.foreign_calls = (
                ToolCall(
                    call_id="a_output_foreign",
                    name="output",
                    arguments={"exec_id": foreign_exec_id},
                ),
                ToolCall(
                    call_id="a_output_missing",
                    name="output",
                    arguments={"exec_id": "missing-execution"},
                ),
                ToolCall(
                    call_id="a_kill_foreign",
                    name="kill",
                    arguments={"exec_id": foreign_exec_id},
                ),
                ToolCall(
                    call_id="a_kill_missing",
                    name="kill",
                    arguments={"exec_id": "missing-execution"},
                ),
            )
            async for event in _tool_completion(self.foreign_calls):
                yield event
            return

        if self.role == "session-a" and stage == 2:
            self.foreign_results = _last_tool_results(request)
            self.ready_for_close.set()
            await self.finish_allowed.wait()
            async for event in _text_completion("Old Session A completed."):
                yield event
            return

        raise AssertionError(f"unexpected {self.role} provider stage: {stage}")


def test_public_runtime_proves_concurrent_session_scheduling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)

    async def scenario() -> None:
        a_started = tmp_path / "runtime-a-started"
        a_release = tmp_path / "runtime-a-release"
        a_queued = tmp_path / "runtime-a-queued"
        a_overflow = tmp_path / "runtime-a-overflow"
        b_started = tmp_path / "runtime-b-started"
        b_release = tmp_path / "runtime-b-release"
        b_queued = tmp_path / "runtime-b-queued"
        fresh_proof = tmp_path / "runtime-a-fresh"
        calls_a = (
            ToolCall(
                call_id="a_exec_running",
                name="exec",
                arguments={
                    "command": _blocking_command(a_started, a_release),
                    "wait_ms": 0,
                },
            ),
            ToolCall(
                call_id="a_exec_queued",
                name="exec",
                arguments={"command": _touch_command(a_queued), "wait_ms": 0},
            ),
            ToolCall(
                call_id="a_exec_overflow",
                name="exec",
                arguments={"command": _touch_command(a_overflow), "wait_ms": 0},
            ),
            ToolCall(
                call_id="a_exec_denied",
                name="exec",
                arguments={"command": "rm denied-proof", "wait_ms": 0},
            ),
        )
        calls_b = (
            ToolCall(
                call_id="b_exec_running",
                name="exec",
                arguments={
                    "command": _blocking_command(b_started, b_release),
                    "wait_ms": 0,
                },
            ),
            ToolCall(
                call_id="b_exec_queued",
                name="exec",
                arguments={"command": _touch_command(b_queued), "wait_ms": 0},
            ),
        )
        provider_a = _CoordinatedProvider("session-a", calls_a)
        provider_b = _CoordinatedProvider("session-b", calls_b)
        provider_a.peer = provider_b
        provider_b.peer = provider_a
        default_provider = ScriptedModelProvider(script=())
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=default_provider,
        )
        turn_a: asyncio.Task[tuple[ModelEvent, ...]] | None = None
        turn_b: asyncio.Task[tuple[ModelEvent, ...]] | None = None
        try:
            user_a = UserMessage.text("Run coordinated Session A work")
            user_b = UserMessage.text("Run coordinated Session B work")
            turn_a = asyncio.create_task(
                _collect_turn(
                    runtime,
                    "session-a",
                    user_a,
                    provider=provider_a,
                )
            )
            turn_b = asyncio.create_task(
                _collect_turn(
                    runtime,
                    "session-b",
                    user_b,
                    provider=provider_b,
                )
            )

            await asyncio.wait_for(provider_a.ready_for_close.wait(), timeout=2)
            await _wait_for_path(a_started)
            await _wait_for_path(b_started)
            assert not a_release.exists() and not b_release.exists()
            assert not a_queued.exists() and not b_queued.exists()
            assert not a_overflow.exists()

            if provider_a.initial_results is None:
                raise AssertionError("Session A initial results were not captured")
            if provider_b.initial_results is None:
                raise AssertionError("Session B initial results were not captured")
            if provider_a.foreign_results is None:
                raise AssertionError("Session A foreign results were not captured")
            initial_a = provider_a.initial_results
            initial_b = provider_b.initial_results
            foreign_a = provider_a.foreign_results

            assert [_result_status(result) for result in initial_a[:3]] == [
                "running",
                "queued",
                "queued",
            ]
            assert _result_error_code(initial_a[3]) == "policy_denied"
            assert [_result_status(result) for result in initial_b] == [
                "running",
                "queued",
            ]
            assert [result.call_id for result in foreign_a] == [
                call.call_id for call in provider_a.foreign_calls
            ]
            assert [_result_error(result) for result in foreign_a] == [
                {
                    "ok": False,
                    "code": "unknown_execution",
                    "message": "execution not found",
                }
            ] * 4

            _assert_request_isolation(
                provider_a.requests,
                own_user=user_a,
                foreign_user=user_b,
            )
            _assert_request_isolation(
                provider_b.requests,
                own_user=user_b,
                foreign_user=user_a,
            )
            assert _tool_result_ids(provider_a.requests[1]) == [
                call.call_id for call in calls_a
            ]
            assert _tool_result_ids(provider_a.requests[2]) == [
                call.call_id for call in provider_a.foreign_calls
            ]
            assert _tool_result_ids(provider_b.requests[1]) == [
                call.call_id for call in calls_b
            ]
            system_a = provider_a.requests[0].messages[0]
            system_b = provider_b.requests[0].messages[0]
            assert isinstance(system_a, SystemMessage)
            assert isinstance(system_b, SystemMessage)
            assert system_a is not system_b

            old_session_a = runtime._sessions["session-a"]
            old_kernel_a = old_session_a.kernel
            old_states_a = tuple(old_kernel_a._executions.values())
            old_handles_a = tuple(state.exec_id for state in old_states_a)
            session_b = runtime._sessions["session-b"]
            kernel_b = session_b.kernel
            running_b_id = str(_result_output(initial_b[0])["exec_id"])
            assert kernel_b._executions[running_b_id].status == "running"

            await runtime.close_session("session-a")
            assert old_kernel_a._closed is True
            assert old_kernel_a._executions == {}
            assert all(
                state.status in {"killed", "exited", "failed"}
                for state in old_states_a
            )
            assert all(
                state.completion_task is None or state.completion_task.done()
                for state in old_states_a
            )
            assert not a_queued.exists() and not a_overflow.exists()
            assert kernel_b._executions[running_b_id].status == "running"

            provider_a.finish_allowed.set()
            events_a = await asyncio.wait_for(turn_a, timeout=0.5)
            assert isinstance(events_a[-1], ModelCompletion)

            b_release.touch()
            await _wait_for_path(b_queued)
            for result in initial_b:
                await _wait_for_terminal_state(
                    kernel_b._executions[str(_result_output(result)["exec_id"])]
                )
            provider_b.finish_allowed.set()
            events_b = await asyncio.wait_for(turn_b, timeout=0.5)
            assert isinstance(events_b[-1], ModelCompletion)
            assert [
                kernel_b._executions[str(_result_output(result)["exec_id"])].status
                for result in initial_b
            ] == ["exited", "exited"]

            old_handle = old_handles_a[0]
            fresh_calls = (
                ToolCall(
                    call_id="fresh_output_old",
                    name="output",
                    arguments={"exec_id": old_handle},
                ),
                ToolCall(
                    call_id="fresh_output_missing",
                    name="output",
                    arguments={"exec_id": "missing-execution"},
                ),
                ToolCall(
                    call_id="fresh_exec",
                    name="exec",
                    arguments={"command": _touch_command(fresh_proof)},
                ),
            )
            fresh_tool_message = AssistantMessage(content=fresh_calls)
            fresh_final = AssistantMessage.text("Fresh Session A completed.")
            fresh_provider = ScriptedModelProvider(
                script=(
                    (
                        *(ToolCallReady(call=call) for call in fresh_calls),
                        ModelCompletion(
                            message=fresh_tool_message,
                            finish_reason="tool_calls",
                        ),
                    ),
                    (
                        TextDelta(text="Fresh Session A completed."),
                        ModelCompletion(
                            message=fresh_final,
                            finish_reason="stop",
                        ),
                    ),
                )
            )
            fresh_user = UserMessage.text("Start a fresh Session A")
            fresh_events = await _collect_turn(
                runtime,
                "session-a",
                fresh_user,
                provider=fresh_provider,
            )
            fresh_results = _last_tool_results(fresh_provider.requests[1])

            assert isinstance(fresh_events[-1], ModelCompletion)
            assert len(fresh_provider.requests) == 2
            fresh_system = fresh_provider.requests[0].messages[0]
            assert isinstance(fresh_system, SystemMessage)
            assert fresh_system is not system_a
            assert fresh_provider.requests[0].messages == (
                fresh_system,
                fresh_user,
            )
            assert [_result_error(result) for result in fresh_results[:2]] == [
                {
                    "ok": False,
                    "code": "unknown_execution",
                    "message": "execution not found",
                }
            ] * 2
            assert _result_status(fresh_results[2]) == "exited"
            fresh_exec_id = str(_result_output(fresh_results[2])["exec_id"])
            assert fresh_exec_id not in old_handles_a
            assert fresh_proof.exists()
            fresh_session = runtime._sessions["session-a"]
            assert fresh_session is not old_session_a
            assert fresh_session.kernel is not old_kernel_a
            assert (
                fresh_session.kernel._executions[fresh_exec_id].submission_sequence
                == 0
            )
            fresh_provider.assert_exhausted()
            default_provider.assert_exhausted()

            all_requests = (
                *provider_a.requests,
                *provider_b.requests,
                *fresh_provider.requests,
            )
            for request in all_requests:
                assert [tool.name for tool in request.tools] == [
                    "exec",
                    "output",
                    "kill",
                ]

            active_sessions = tuple(runtime._sessions.values())
            active_kernels = tuple(session.kernel for session in active_sessions)
            await runtime.close()

            assert runtime.closed
            assert runtime._sessions == {}
            assert all(kernel._closed for kernel in active_kernels)
            assert all(not kernel._executions for kernel in active_kernels)
        finally:
            provider_a.finish_allowed.set()
            provider_b.finish_allowed.set()
            a_release.touch(exist_ok=True)
            b_release.touch(exist_ok=True)
            for turn in (turn_a, turn_b):
                if turn is not None and not turn.done():
                    turn.cancel()
            for turn in (turn_a, turn_b):
                if turn is not None:
                    with suppress(asyncio.CancelledError, Exception):
                        await turn
            await runtime.close()

    asyncio.run(scenario())


async def _tool_completion(
    calls: tuple[ToolCall, ...],
) -> AsyncIterator[ModelEvent]:
    for call in calls:
        yield ToolCallReady(call=call)
    yield ModelCompletion(
        message=AssistantMessage(content=calls),
        finish_reason="tool_calls",
    )


async def _text_completion(text: str) -> AsyncIterator[ModelEvent]:
    yield TextDelta(text=text)
    yield ModelCompletion(
        message=AssistantMessage.text(text),
        finish_reason="stop",
    )


async def _collect_turn(
    runtime: AgentRuntime,
    session_id: str,
    message: UserMessage,
    *,
    provider=None,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(
                session_id,
                message,
                provider=provider,
            )
        ]
    )


def _last_tool_results(request: ModelRequest) -> tuple[ToolResult, ...]:
    message = request.messages[-1]
    if not isinstance(message, ToolResultMessage):
        raise AssertionError("request does not end with Tool Results")
    return message.content


def _tool_result_ids(request: ModelRequest) -> list[str]:
    return [result.call_id for result in _last_tool_results(request)]


def _result_output(result: ToolResult) -> dict[str, object]:
    if not isinstance(result.output, dict):
        raise AssertionError(f"{result.call_id} has no output")
    return result.output


def _result_error(result: ToolResult) -> dict[str, object]:
    if not isinstance(result.error, dict):
        raise AssertionError(f"{result.call_id} has no error")
    return result.error


def _result_status(result: ToolResult) -> object:
    return _result_output(result)["status"]


def _result_error_code(result: ToolResult) -> object:
    return _result_error(result)["code"]


def _assert_request_isolation(
    requests: tuple[ModelRequest, ...],
    *,
    own_user: UserMessage,
    foreign_user: UserMessage,
) -> None:
    for request in requests:
        assert own_user in request.messages
        assert foreign_user not in request.messages


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _blocking_command(started: Path, release: Path) -> str:
    return _python_command(
        "import time\n"
        "from pathlib import Path\n"
        f"started = Path({str(started)!r})\n"
        f"release = Path({str(release)!r})\n"
        "started.touch()\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)"
    )


def _touch_command(path: Path) -> str:
    return _python_command(f"from pathlib import Path; Path({str(path)!r}).touch()")


async def _wait_for_path(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} was not created")


async def _wait_for_terminal_state(
    state: _ExecutionState,
) -> None:
    for _ in range(100):
        if state.is_terminal:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Execution {state.exec_id} did not terminate")


def _deny_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("network access is forbidden in this scenario")
