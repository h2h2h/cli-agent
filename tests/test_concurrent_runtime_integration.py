import asyncio
import shlex
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction
from policy_fakes import _AskExecutablePolicy

from cli_agent.presets import open_default_runtime
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
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

_user_interaction = _ScriptedInteraction("deny")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


class _CoordinatedProvider:
    """Serve one tool-call turn, capture results, then wait for release."""

    def __init__(self, calls: tuple[ToolCall, ...]) -> None:
        self.calls = calls
        self.initial_results_ready = asyncio.Event()
        self.finish_allowed = asyncio.Event()
        self.initial_results: tuple[ToolResult, ...] | None = None
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
            async for event in _tool_completion(self.calls):
                yield event
            return

        if stage == 1:
            self.initial_results = _last_tool_results(request)
            self.initial_results_ready.set()
            await self.finish_allowed.wait()
            async for event in _text_completion("Session completed."):
                yield event
            return

        raise AssertionError(f"unexpected provider stage: {stage}")


def test_public_runtime_proves_single_active_binding_isolation(
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
        provider_a = _CoordinatedProvider(calls_a)
        default_provider = ScriptedModelProvider(script=())
        runtime = await open_default_runtime(
            interaction=_user_interaction,
            workspace=tmp_path,
            provider=default_provider,
            policy=_AskExecutablePolicy(
                frozenset({"rm"}),
                rule_id="test.ask-rm",
                reason="rm requires Host approval",
            ),
            context_policy=_context_policy,
        )
        turn_a: asyncio.Task[tuple[ModelEvent, ...]] | None = None
        try:
            await runtime.new_session(provider=provider_a)
            user_a = UserMessage.text("Run coordinated Session work")
            turn_a = asyncio.create_task(_collect_turn(runtime, user_a))

            await asyncio.wait_for(provider_a.initial_results_ready.wait(), timeout=2)
            await _wait_for_path(a_started)
            assert not a_release.exists()
            assert not a_queued.exists() and not a_overflow.exists()

            if provider_a.initial_results is None:
                raise AssertionError("initial results were not captured")
            initial = provider_a.initial_results

            assert [_result_status(result) for result in initial[:3]] == [
                "running",
                "queued",
                "queued",
            ]
            assert _result_error_code(initial[3]) == "policy_denied"
            assert _tool_result_ids(provider_a.requests[1]) == [
                call.call_id for call in calls_a
            ]
            _assert_request_isolation(provider_a.requests, own_user=user_a)
            system_a = provider_a.requests[0].messages[0]
            assert isinstance(system_a, SystemMessage)

            old_kernel_a = runtime._binding.kernel
            old_states_a = tuple(old_kernel_a._executions.values())
            old_handles_a = tuple(state.exec_id for state in old_states_a)
            running_a_id = str(_result_output(initial[0])["exec_id"])
            assert old_kernel_a._executions[running_a_id].status == "running"

            await runtime.detach_session()
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

            assert turn_a.cancelled()
            with pytest.raises(asyncio.CancelledError):
                await turn_a
            turn_a = None

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
                    call_id="fresh_kill_old",
                    name="kill",
                    arguments={"exec_id": old_handle},
                ),
                ToolCall(
                    call_id="fresh_exec",
                    name="exec",
                    arguments={"command": _touch_command(fresh_proof)},
                ),
            )
            fresh_tool_message = AssistantMessage(content=fresh_calls)
            fresh_final = AssistantMessage.text("Fresh Session completed.")
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
                        TextDelta(text="Fresh Session completed."),
                        ModelCompletion(
                            message=fresh_final,
                            finish_reason="stop",
                        ),
                    ),
                )
            )
            fresh_user = UserMessage.text("Start a fresh Session")
            await runtime.new_session(provider=fresh_provider)
            fresh_events = await _collect_turn(runtime, fresh_user)
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
            assert [_result_error(result) for result in fresh_results[:3]] == [
                {
                    "ok": False,
                    "code": "unknown_execution",
                    "message": "execution not found",
                }
            ] * 3
            assert _result_status(fresh_results[3]) == "exited"
            fresh_exec_id = str(_result_output(fresh_results[3])["exec_id"])
            assert fresh_exec_id not in old_handles_a
            assert fresh_proof.exists()
            fresh_kernel = runtime._binding.kernel
            assert fresh_kernel is not old_kernel_a
            assert (
                fresh_kernel._executions[fresh_exec_id].submission_sequence == 0
            )
            fresh_provider.assert_exhausted()
            default_provider.assert_exhausted()

            for request in provider_a.requests:
                assert [tool.name for tool in request.tools] == [
                    "exec",
                    "output",
                    "kill",
                ]
            for request in fresh_provider.requests:
                assert [tool.name for tool in request.tools] == [
                    "exec",
                    "output",
                    "kill",
                ]

            await runtime.close()

            assert runtime.closed
            assert runtime._binding is None
            assert fresh_kernel._closed
            assert not fresh_kernel._executions
        finally:
            provider_a.finish_allowed.set()
            a_release.touch(exist_ok=True)
            if turn_a is not None and not turn_a.done():
                turn_a.cancel()
            if turn_a is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await turn_a
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
    message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(message)
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
) -> None:
    for request in requests:
        assert own_user in request.messages


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


def _deny_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("network access is forbidden in this scenario")
