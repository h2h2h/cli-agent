import asyncio
import shlex
import sys
from pathlib import Path

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.command_parser import CommandParseResult
from cli_agent.runtime._environment.execution import _ExecutionState
from cli_agent.runtime._environment.policy import ExecutionDecision

_UNKNOWN_EXECUTION = {
    "ok": False,
    "code": "unknown_execution",
    "message": "execution not found",
}
_PRIVATE_SNAPSHOT_FIELDS = {
    "lane",
    "pid",
    "process_id",
    "session_id",
    "submission_sequence",
}


class _CountingPolicy:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(
        self,
        command: CommandParseResult,
    ) -> ExecutionDecision:
        self.calls += 1
        return ExecutionDecision.allow(command)


class _BlockingPolicy:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(
        self,
        command: CommandParseResult,
    ) -> ExecutionDecision:
        self.entered.set()
        await self.release.wait()
        return ExecutionDecision.allow(command)


def test_foreign_and_missing_handles_are_indistinguishable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        policy = _CountingPolicy()
        running_started = tmp_path / "private-running-started"
        running_release = tmp_path / "private-running-release"
        queued_proof = tmp_path / "private-queued-proof"
        owner = EnvironmentKernel(
            tmp_path,
            policy=policy,
            queue_limit=1,
        )
        stranger = EnvironmentKernel(
            tmp_path,
            policy=policy,
            queue_limit=1,
        )
        replacement: EnvironmentKernel | None = None
        try:
            terminal = _output(
                await owner.dispatch(
                    ToolCall(
                        call_id="exec_private_terminal",
                        name="exec",
                        arguments={"command": _python_command("pass")},
                    )
                )
            )
            running = _output(
                await owner.dispatch(
                    ToolCall(
                        call_id="exec_private_running",
                        name="exec",
                        arguments={
                            "command": _blocking_command(
                                running_started,
                                running_release,
                            ),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(running_started)
            queued = _output(
                await owner.dispatch(
                    ToolCall(
                        call_id="exec_private_queued",
                        name="exec",
                        arguments={
                            "command": _touch_command(queued_proof),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            assert policy.calls == 3

            for index, snapshot in enumerate((terminal, running, queued)):
                assert _PRIVATE_SNAPSHOT_FIELDS.isdisjoint(snapshot)
                for tool_name in ("output", "kill"):
                    foreign = await stranger.dispatch(
                        ToolCall(
                            call_id=f"{tool_name}_foreign_{index}",
                            name=tool_name,
                            arguments={"exec_id": snapshot["exec_id"]},
                        )
                    )
                    missing = await stranger.dispatch(
                        ToolCall(
                            call_id=f"{tool_name}_missing_{index}",
                            name=tool_name,
                            arguments={"exec_id": f"missing-{index}"},
                        )
                    )
                    assert _error(foreign) == _UNKNOWN_EXECUTION
                    assert _error(missing) == _UNKNOWN_EXECUTION

            assert policy.calls == 3
            assert (
                _output(
                    await owner.dispatch(
                        ToolCall(
                            call_id="output_owned_terminal",
                            name="output",
                            arguments={"exec_id": terminal["exec_id"]},
                        )
                    )
                )["status"]
                == "exited"
            )
            assert (
                _output(
                    await owner.dispatch(
                        ToolCall(
                            call_id="output_owned_running",
                            name="output",
                            arguments={"exec_id": running["exec_id"]},
                        )
                    )
                )["status"]
                == "running"
            )
            assert (
                _output(
                    await owner.dispatch(
                        ToolCall(
                            call_id="output_owned_queued",
                            name="output",
                            arguments={"exec_id": queued["exec_id"]},
                        )
                    )
                )["status"]
                == "queued"
            )
            assert not queued_proof.exists()

            running_release.touch()
            assert (
                await _read_until_terminal(
                    owner,
                    str(running["exec_id"]),
                )
            )["status"] == "exited"
            assert (
                await _read_until_terminal(
                    owner,
                    str(queued["exec_id"]),
                )
            )["status"] == "exited"
            assert queued_proof.exists()

            old_handles = tuple(
                str(snapshot["exec_id"]) for snapshot in (terminal, running, queued)
            )
            await owner.close()
            replacement = EnvironmentKernel(
                tmp_path,
                policy=policy,
                queue_limit=1,
            )
            for index, exec_id in enumerate(old_handles):
                for tool_name in ("output", "kill"):
                    invalidated = await replacement.dispatch(
                        ToolCall(
                            call_id=f"{tool_name}_invalidated_{index}",
                            name=tool_name,
                            arguments={"exec_id": exec_id},
                        )
                    )
                    assert _error(invalidated) == _UNKNOWN_EXECUTION
            assert policy.calls == 3
        finally:
            await owner.close()
            await stranger.close()
            if replacement is not None:
                await replacement.close()

    asyncio.run(scenario())


def test_sessions_run_shell_work_concurrently_without_cross_session_hol(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        a_started = tmp_path / "session-a-started"
        a_release = tmp_path / "session-a-release"
        a_promoted = tmp_path / "session-a-promoted"
        shared = tmp_path / "shared-effect"
        b_started = tmp_path / "session-b-started"
        b_release = tmp_path / "session-b-release"
        b_promoted = tmp_path / "session-b-promoted"
        b_observed = tmp_path / "session-b-observed"
        binding_a = EnvironmentKernel(tmp_path, queue_limit=1)
        binding_b = EnvironmentKernel(tmp_path, queue_limit=1)
        try:
            running_a = _output(
                await binding_a.dispatch(
                    ToolCall(
                        call_id="exec_a_running",
                        name="exec",
                        arguments={
                            "command": _blocking_command(a_started, a_release),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(a_started)
            queued_a = _output(
                await binding_a.dispatch(
                    ToolCall(
                        call_id="exec_a_queued",
                        name="exec",
                        arguments={
                            "command": _python_command(
                                "from pathlib import Path; "
                                "Path('shared-effect').write_text('from-a'); "
                                "Path('session-a-promoted').touch()"
                            ),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            overflow_a = await binding_a.dispatch(
                ToolCall(
                    call_id="exec_a_overflow",
                    name="exec",
                    arguments={"command": _python_command("pass"), "wait_ms": 0},
                )
            )
            assert queued_a["status"] == "queued"
            assert _error(overflow_a)["code"] == "queue_full"

            running_b = _output(
                await binding_b.dispatch(
                    ToolCall(
                        call_id="exec_b_running",
                        name="exec",
                        arguments={
                            "command": _blocking_command(b_started, b_release),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(b_started)
            queued_b = _output(
                await binding_b.dispatch(
                    ToolCall(
                        call_id="exec_b_queued",
                        name="exec",
                        arguments={
                            "command": _python_command(
                                "from pathlib import Path; "
                                "Path('session-b-observed').write_text("
                                "Path('shared-effect').read_text()); "
                                "Path('session-b-promoted').touch()"
                            ),
                            "wait_ms": 0,
                        },
                    )
                )
            )

            assert running_a["status"] == "running"
            assert running_b["status"] == "running"
            assert queued_b["status"] == "queued"
            assert a_started.exists() and b_started.exists()
            assert not a_release.exists() and not b_release.exists()
            assert not a_promoted.exists() and not b_promoted.exists()

            assert [
                state.submission_sequence
                for state in binding_a._executions.values()
            ] == [0, 1]
            assert [
                state.submission_sequence
                for state in binding_b._executions.values()
            ] == [0, 1]

            a_release.touch()
            assert (
                await _read_until_terminal(
                    binding_a,
                    str(running_a["exec_id"]),
                )
            )["status"] == "exited"
            assert (
                await _read_until_terminal(
                    binding_a,
                    str(queued_a["exec_id"]),
                )
            )["status"] == "exited"
            assert a_promoted.exists()
            assert shared.read_text() == "from-a"
            assert (
                _output(
                    await binding_b.dispatch(
                        ToolCall(
                            call_id="output_b_still_running",
                            name="output",
                            arguments={"exec_id": running_b["exec_id"]},
                        )
                    )
                )["status"]
                == "running"
            )
            assert not b_promoted.exists()

            b_release.touch()
            assert (
                await _read_until_terminal(
                    binding_b,
                    str(running_b["exec_id"]),
                )
            )["status"] == "exited"
            assert (
                await _read_until_terminal(
                    binding_b,
                    str(queued_b["exec_id"]),
                )
            )["status"] == "exited"
            assert b_promoted.exists()
            assert b_observed.read_text() == "from-a"
        finally:
            await binding_a.close()
            await binding_b.close()

    asyncio.run(scenario())


def test_close_prevents_admission_after_async_policy_returns(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        policy = _BlockingPolicy()
        must_not_run = tmp_path / "close-policy-must-not-run"
        kernel = EnvironmentKernel(tmp_path, policy=policy)
        dispatch = asyncio.create_task(
            kernel.dispatch(
                ToolCall(
                    call_id="exec_during_close",
                    name="exec",
                    arguments={"command": _touch_command(must_not_run)},
                )
            )
        )
        await policy.entered.wait()

        await kernel.close()
        policy.release.set()
        result = await asyncio.wait_for(dispatch, timeout=0.5)

        assert _error(result) == {
            "ok": False,
            "code": "internal",
            "message": "environment session is closed",
        }
        assert not must_not_run.exists()
        assert kernel._executions == {}
        await kernel.close()

    asyncio.run(scenario())


def test_close_cancels_owned_work_and_recreation_is_fresh(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        closing_started = tmp_path / "closing-session-started"
        closing_release = tmp_path / "closing-session-release"
        queued_must_not_run = tmp_path / "closing-queued-must-not-run"
        peer_started = tmp_path / "peer-session-started"
        peer_release = tmp_path / "peer-session-release"
        closing = EnvironmentKernel(tmp_path, queue_limit=1)
        peer = EnvironmentKernel(tmp_path, queue_limit=1)
        replacement: EnvironmentKernel | None = None
        try:
            running = _output(
                await closing.dispatch(
                    ToolCall(
                        call_id="exec_closing_running",
                        name="exec",
                        arguments={
                            "command": _blocking_command(
                                closing_started,
                                closing_release,
                            ),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(closing_started)
            exec_waiter = asyncio.create_task(
                closing.dispatch(
                    ToolCall(
                        call_id="exec_closing_queued",
                        name="exec",
                        arguments={
                            "command": _touch_command(queued_must_not_run),
                            "wait_ms": 5_000,
                        },
                    )
                )
            )
            queued_state = await _wait_for_queued_state(closing)
            running_state = closing._executions[str(running["exec_id"])]
            output_waiter = asyncio.create_task(
                closing.dispatch(
                    ToolCall(
                        call_id="output_closing_queued",
                        name="output",
                        arguments={
                            "exec_id": queued_state.exec_id,
                            "wait_ms": 5_000,
                        },
                    )
                )
            )

            peer_running = _output(
                await peer.dispatch(
                    ToolCall(
                        call_id="exec_peer_running",
                        name="exec",
                        arguments={
                            "command": _blocking_command(peer_started, peer_release),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(peer_started)
            await asyncio.sleep(0)

            await asyncio.wait_for(closing.close(), timeout=2)
            exec_result = _output(await asyncio.wait_for(exec_waiter, timeout=0.5))
            output_result = _output(await asyncio.wait_for(output_waiter, timeout=0.5))

            assert exec_result["status"] == "killed"
            assert output_result == exec_result
            assert running_state.status == "killed"
            assert running_state.completion_task is not None
            assert running_state.completion_task.done()
            assert queued_state.status == "killed"
            assert queued_state.completion_task is None
            assert not queued_must_not_run.exists()
            assert closing._executions == {}
            assert (
                _output(
                    await peer.dispatch(
                        ToolCall(
                            call_id="output_peer_unaffected",
                            name="output",
                            arguments={"exec_id": peer_running["exec_id"]},
                        )
                    )
                )["status"]
                == "running"
            )

            closed_binding_result = await closing.dispatch(
                ToolCall(
                    call_id="output_closed_binding",
                    name="output",
                    arguments={"exec_id": running["exec_id"]},
                )
            )
            assert _error(closed_binding_result)["code"] == "internal"
            await closing.close()

            replacement = EnvironmentKernel(
                tmp_path,
                queue_limit=1,
            )
            for index, old_exec_id in enumerate(
                (str(running["exec_id"]), queued_state.exec_id)
            ):
                invalidated = await replacement.dispatch(
                    ToolCall(
                        call_id=f"output_old_handle_{index}",
                        name="output",
                        arguments={"exec_id": old_exec_id},
                    )
                )
                assert _error(invalidated) == _UNKNOWN_EXECUTION

            fresh = _output(
                await replacement.dispatch(
                    ToolCall(
                        call_id="exec_fresh_session",
                        name="exec",
                        arguments={"command": _python_command("pass")},
                    )
                )
            )
            assert fresh["status"] == "exited"
            fresh_state = replacement._executions[str(fresh["exec_id"])]
            assert fresh_state.submission_sequence == 0
            assert fresh["exec_id"] not in {
                running["exec_id"],
                queued_state.exec_id,
            }

            peer_release.touch()
            assert (
                await _read_until_terminal(
                    peer,
                    str(peer_running["exec_id"]),
                )
            )["status"] == "exited"
        finally:
            closing_release.touch()
            peer_release.touch()
            await closing.close()
            await peer.close()
            if replacement is not None:
                await replacement.close()

    asyncio.run(scenario())


def test_close_wakes_concurrent_running_cancellation_waiter(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = tmp_path / "close-kill-waiter-started"
        kernel = EnvironmentKernel(tmp_path)
        running = _output(
            await kernel.dispatch(
                ToolCall(
                    call_id="exec_close_kill_waiter",
                    name="exec",
                    arguments={
                        "command": _stubborn_command(started),
                        "wait_ms": 0,
                    },
                )
            )
        )
        await _wait_for_path(started)
        state = kernel._executions[str(running["exec_id"])]

        kill_waiter = asyncio.create_task(
            kernel.dispatch(
                ToolCall(
                    call_id="kill_during_close",
                    name="kill",
                    arguments={"exec_id": running["exec_id"]},
                )
            )
        )
        await asyncio.sleep(0)
        assert not kill_waiter.done()

        await asyncio.wait_for(kernel.close(), timeout=2)
        killed = _output(await asyncio.wait_for(kill_waiter, timeout=0.5))

        assert killed["status"] == "killed"
        assert killed["is_terminal"] is True
        assert state.completion_task is not None
        assert state.completion_task.done()
        assert kernel._executions == {}
        await kernel.close()

    asyncio.run(scenario())


def test_each_kernel_close_cancels_its_running_and_queued_work(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernels = (
            EnvironmentKernel(tmp_path, queue_limit=1),
            EnvironmentKernel(tmp_path, queue_limit=1),
        )
        states = []
        queued_proofs = []
        for index, kernel in enumerate(kernels):
            started = tmp_path / f"kernel-close-{index}-started"
            release = tmp_path / f"kernel-close-{index}-release"
            queued_proof = tmp_path / f"kernel-close-{index}-queued"
            running = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id=f"exec_kernel_close_running_{index}",
                        name="exec",
                        arguments={
                            "command": _blocking_command(started, release),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(started)
            queued = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id=f"exec_kernel_close_queued_{index}",
                        name="exec",
                        arguments={
                            "command": _touch_command(queued_proof),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            states.append(
                (
                    kernel._executions[str(running["exec_id"])],
                    kernel._executions[str(queued["exec_id"])],
                )
            )
            queued_proofs.append(queued_proof)

        for kernel in kernels:
            await kernel.close()
            await kernel.close()

        assert all(kernel._executions == {} for kernel in kernels)
        for running_state, queued_state in states:
            assert running_state.status == "killed"
            assert running_state.completion_task is not None
            assert running_state.completion_task.done()
            assert queued_state.status == "killed"
            assert queued_state.completion_task is None
        assert not any(path.exists() for path in queued_proofs)
        for index, kernel in enumerate(kernels):
            closed = await kernel.dispatch(
                ToolCall(
                    call_id=f"exec_after_kernel_close_{index}",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )
            assert _error(closed)["code"] == "internal"

    asyncio.run(scenario())


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


def _stubborn_command(started: Path) -> str:
    return _python_command(
        "import signal\n"
        "import time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(started)!r}).touch()\n"
        "while True:\n"
        "    time.sleep(0.1)"
    )


def _output(result: ToolResult) -> dict[str, object]:
    assert isinstance(result.output, dict)
    return result.output


def _error(result: ToolResult) -> dict[str, object]:
    assert isinstance(result.error, dict)
    return result.error


async def _read_until_terminal(
    binding: EnvironmentKernel,
    exec_id: str,
) -> dict[str, object]:
    chunks: list[object] = []
    cursor = 0
    for index in range(10):
        snapshot = _output(
            await binding.dispatch(
                ToolCall(
                    call_id=f"output_terminal_{exec_id}_{index}",
                    name="output",
                    arguments={
                        "exec_id": exec_id,
                        "cursor": cursor,
                        "wait_ms": 500,
                    },
                )
            )
        )
        chunks.extend(snapshot["chunks"])
        cursor = int(snapshot["next_cursor"])
        if snapshot["is_terminal"]:
            snapshot["chunks"] = chunks
            return snapshot
    raise AssertionError(f"Execution {exec_id} did not terminate")


async def _wait_for_path(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} was not created")


async def _wait_for_queued_state(
    kernel: EnvironmentKernel,
) -> _ExecutionState:
    for _ in range(100):
        for state in kernel._executions.values():
            if state.status == "queued":
                return state
        await asyncio.sleep(0.01)
    raise AssertionError("queued Execution State was not created")
