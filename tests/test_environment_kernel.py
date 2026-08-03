import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest
from policy_fakes import _AskExecutablePolicy, _DenyExecutablePolicy

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._capability.command_parser import (
    ShellParseResult,
    parse_shell_ast,
)
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.commands import (
    _builtin_custom_commands,
    _CustomCommandRegistry,
    _ShellCommand,
)
from cli_agent.runtime._environment.execution_state import _ExecutionState
from cli_agent.runtime._environment.handlers.shell import _ShellHandler
from cli_agent.runtime._environment.policy import (
    PolicyAction,
    PolicyEvaluation,
)
from cli_agent.runtime._environment.routing import _CommandRouter
from cli_agent.runtime._environment.scheduler import _ExecutionScheduler


def _router() -> _CommandRouter:
    registry = _CustomCommandRegistry(_builtin_custom_commands())
    return _CommandRouter(
        shell_command=_ShellCommand(prepare=_ShellHandler().prepare),
        custom_registry=registry,
    )


def test_executes_short_command_and_retains_ordered_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel
        command = _python_command(
            "import os, sys; "
            "print(os.getcwd()); "
            "sys.stdout.flush(); "
            "sys.stderr.write('warning\\n')"
        )

        executed = await binding.dispatch(
            ToolCall(
                call_id="exec_1",
                name="exec",
                arguments={"command": command},
            )
        )
        snapshot = _output(executed)

        assert executed.call_id == "exec_1"
        assert executed.error is None
        assert snapshot["ok"] is True
        assert snapshot["status"] == "exited"
        assert snapshot["exit_code"] == 0
        assert snapshot["is_terminal"] is True
        assert snapshot["truncated"] is False
        assert snapshot["available_from"] == 0
        assert isinstance(snapshot["exec_id"], str)
        assert snapshot["exec_id"]
        assert "session_id" not in snapshot

        chunks = snapshot["chunks"]
        assert isinstance(chunks, list)
        assert [chunk["cursor"] for chunk in chunks] == list(range(len(chunks)))
        assert str(tmp_path) in _stream_text(chunks, "stdout")
        assert "warning\n" in _stream_text(chunks, "stderr")
        assert snapshot["next_cursor"] == len(chunks)
        json.dumps(snapshot)

        retained = await binding.dispatch(
            ToolCall(
                call_id="output_1",
                name="output",
                arguments={"exec_id": snapshot["exec_id"]},
            )
        )
        assert _output(retained) == snapshot

        killed = await binding.dispatch(
            ToolCall(
                call_id="kill_1",
                name="kill",
                arguments={"exec_id": snapshot["exec_id"]},
            )
        )
        killed_again = await binding.dispatch(
            ToolCall(
                call_id="kill_2",
                name="kill",
                arguments={"exec_id": snapshot["exec_id"]},
            )
        )
        assert _output(killed) == snapshot
        assert _output(killed_again) == snapshot

    asyncio.run(scenario())


def test_reports_nonzero_exit_as_terminal_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(tmp_path)

        result = await binding.dispatch(
            ToolCall(
                call_id="exec_nonzero",
                name="exec",
                arguments={
                    "command": _python_command(
                        "import sys; "
                        "print('before failure'); "
                        "sys.stderr.write('failure\\n'); "
                        "sys.exit(7)"
                    )
                },
            )
        )
        snapshot = _output(result)

        assert result.error is None
        assert snapshot["ok"] is True
        assert snapshot["status"] == "failed"
        assert snapshot["exit_code"] == 7
        assert snapshot["is_terminal"] is True
        chunks = snapshot["chunks"]
        assert isinstance(chunks, list)
        assert "before failure\n" in _stream_text(chunks, "stdout")
        assert "failure\n" in _stream_text(chunks, "stderr")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command", ("rm proof.txt", '"rm" proof.txt', "/bin/rm proof.txt")
)
def test_ask_without_interaction_fails_closed_before_execution(
    tmp_path: Path,
    command: str,
) -> None:
    async def scenario() -> None:
        proof = tmp_path / "proof.txt"
        proof.write_text("preserved")
        binding = EnvironmentKernel(
            tmp_path,
            policy=_AskExecutablePolicy(
                frozenset({"rm"}),
                rule_id="test.ask-rm",
                reason="rm requires Host approval",
            ),
        )

        result = await binding.dispatch(
            ToolCall(
                call_id="denied_rm",
                name="exec",
                arguments={"command": command},
            )
        )

        assert _error(result) == {
            "ok": False,
            "code": "policy_denied",
            "message": "execution requires user interaction but none is configured",
        }
        assert proof.read_text() == "preserved"

    asyncio.run(scenario())


def test_policy_failure_fails_closed_without_starting_command(tmp_path: Path) -> None:
    class FailingPolicy:
        async def evaluate(
            self,
            command: ShellParseResult,
        ) -> object:
            if "proof" in command.raw_command:
                raise RuntimeError(command.raw_command)
            return PolicyEvaluation(
                action=PolicyAction.ALLOW,
                rule_id="test.allow",
            )

    diagnostics: list[object] = []

    async def scenario() -> None:
        proof = tmp_path / "proof.txt"
        binding = EnvironmentKernel(
            tmp_path,
            policy=FailingPolicy(),  # type: ignore[arg-type]
            on_diagnostic=diagnostics.append,
        )

        result = await binding.dispatch(
            ToolCall(
                call_id="failed_policy",
                name="exec",
                arguments={
                    "command": _python_command(
                        f"from pathlib import Path; Path({str(proof)!r}).touch()"
                    )
                },
            )
        )

        assert _error(result) == {
            "ok": False,
            "code": "policy_denied",
            "message": "execution policy failed closed",
        }
        assert not proof.exists()

        still_usable = await binding.dispatch(
            ToolCall(
                call_id="policy_failure_session_usable",
                name="exec",
                arguments={"command": _python_command("pass")},
            )
        )
        assert _output(still_usable)["status"] == "exited"

    asyncio.run(scenario())

    assert [d.kind for d in diagnostics] == ["execution_policy.failed"]
    assert "RuntimeError" in diagnostics[0].detail["exception"]


def test_policy_invalid_evaluation_fails_closed_with_diagnostic(
    tmp_path: Path,
) -> None:
    class InvalidPolicy:
        async def evaluate(
            self,
            command: ShellParseResult,
        ) -> object:
            del command
            return "not an evaluation"

    diagnostics: list[object] = []

    async def scenario() -> None:
        binding = EnvironmentKernel(
            tmp_path,
            policy=InvalidPolicy(),  # type: ignore[arg-type]
            on_diagnostic=diagnostics.append,
        )

        result = await binding.dispatch(
            ToolCall(
                call_id="invalid_policy",
                name="exec",
                arguments={"command": "pwd"},
            )
        )

        assert _error(result) == {
            "ok": False,
            "code": "policy_denied",
            "message": "execution policy failed closed",
        }
        assert binding._executions == {}

    asyncio.run(scenario())

    assert [d.kind for d in diagnostics] == ["execution_policy.invalid_evaluation"]


def test_policy_denial_reports_the_policy_reason(tmp_path: Path) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(
            tmp_path,
            policy=_DenyExecutablePolicy(
                frozenset({"rm"}),
                reason="direct invocation of 'rm' is denied by policy",
            ),
        )

        result = await binding.dispatch(
            ToolCall(
                call_id="denied_rm_policy",
                name="exec",
                arguments={"command": "rm proof.txt"},
            )
        )

        assert _error(result) == {
            "ok": False,
            "code": "policy_denied",
            "message": "direct invocation of 'rm' is denied by policy",
        }
        assert binding._executions == {}

    asyncio.run(scenario())


def test_one_policy_hook_evaluates_custom_and_shell_fallback(
    tmp_path: Path,
) -> None:
    class CountingPolicy:
        def __init__(self) -> None:
            self.evaluated: list[str] = []

        async def evaluate(
            self,
            command: ShellParseResult,
        ) -> PolicyEvaluation:
            self.evaluated.append(command.raw_command)
            return PolicyEvaluation(
                action=PolicyAction.ALLOW,
                rule_id="test.allow",
            )

    async def scenario() -> None:
        policy = CountingPolicy()
        binding = EnvironmentKernel(tmp_path, policy=policy)
        try:
            exported = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_export",
                        name="exec",
                        arguments={"command": "export HOOK=value"},
                    )
                )
            )
            shell = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_shell",
                        name="exec",
                        arguments={"command": "pwd"},
                    )
                )
            )

            assert exported["status"] == "exited"
            assert shell["status"] == "exited"
            assert policy.evaluated == ["export HOOK=value", "pwd"]
        finally:
            await binding.close()

    asyncio.run(scenario())


def test_policy_none_skips_evaluation_entirely(tmp_path: Path) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(tmp_path)

        result = await binding.dispatch(
            ToolCall(
                call_id="no_policy_rm",
                name="exec",
                arguments={"command": "touch proof.txt"},
            )
        )

        assert result.error is None
        assert _output(result)["status"] == "exited"
        assert (tmp_path / "proof.txt").exists()

    asyncio.run(scenario())


def test_policy_evaluation_carries_no_parsed_command(tmp_path: Path) -> None:
    evaluated: list[str] = []

    class RecordingPolicy:
        async def evaluate(
            self,
            command: ShellParseResult,
        ) -> PolicyEvaluation:
            evaluated.append(command.raw_command)
            return PolicyEvaluation(
                action=PolicyAction.ALLOW,
                rule_id="test.allow",
            )

    async def scenario() -> None:
        binding = EnvironmentKernel(
            tmp_path,
            policy=RecordingPolicy(),
        )

        result = await binding.dispatch(
            ToolCall(
                call_id="policy_executes_original",
                name="exec",
                arguments={"command": "pwd"},
            )
        )

        assert _output(result)["status"] == "exited"

    asyncio.run(scenario())

    assert tuple(PolicyEvaluation.__dataclass_fields__) == (
        "action",
        "rule_id",
        "reason",
    )
    assert evaluated == ["pwd"]


def test_wait_timeout_returns_running_execution_and_incremental_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel
        try:
            started = await binding.dispatch(
                ToolCall(
                    call_id="exec_long",
                    name="exec",
                    arguments={
                        "command": _python_command(
                            "import time; "
                            "print('first', flush=True); "
                            "time.sleep(0.15); "
                            "print('second', flush=True)"
                        ),
                        "wait_ms": 10,
                    },
                )
            )
            initial = _output(started)

            assert initial["status"] == "running"
            assert initial["exit_code"] is None
            assert initial["is_terminal"] is False

            first_read = await binding.dispatch(
                ToolCall(
                    call_id="output_first",
                    name="output",
                    arguments={
                        "exec_id": initial["exec_id"],
                        "cursor": 0,
                        "wait_ms": 500,
                    },
                )
            )
            first_snapshot = _output(first_read)
            cursor = first_snapshot["next_cursor"]
            assert isinstance(cursor, int)
            assert "first\n" in _stream_text(first_snapshot["chunks"], "stdout")

            repeated = await binding.dispatch(
                ToolCall(
                    call_id="output_repeat",
                    name="output",
                    arguments={
                        "exec_id": initial["exec_id"],
                        "cursor": 0,
                    },
                )
            )
            repeated_snapshot = _output(repeated)
            assert (
                repeated_snapshot["chunks"][: len(first_snapshot["chunks"])]
                == (first_snapshot["chunks"])
            )

            terminal = await _read_until_terminal(
                binding,
                str(initial["exec_id"]),
                cursor=cursor,
            )
            assert terminal["status"] == "exited"
            assert terminal["exit_code"] == 0
            assert terminal["is_terminal"] is True
            assert "second\n" in _stream_text(terminal["chunks"], "stdout")
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_schedules_shell_executions_fifo_with_one_running_per_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        order = tmp_path / "order.txt"
        release = tmp_path / "release-first"
        first_command = _python_command(
            "import time\n"
            "from pathlib import Path\n"
            "order = Path('order.txt')\n"
            "release = Path('release-first')\n"
            "order.write_text('first-start\\n')\n"
            "while not release.exists():\n"
            "    time.sleep(0.01)\n"
            "order.write_text(order.read_text() + 'first-end\\n')"
        )
        second_command = _python_command(
            "from pathlib import Path; "
            "order = Path('order.txt'); "
            "order.write_text(order.read_text() + 'second\\n')"
        )
        third_command = _python_command(
            "from pathlib import Path; "
            "order = Path('order.txt'); "
            "order.write_text(order.read_text() + 'third\\n')"
        )
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel
        try:
            first = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_first",
                        name="exec",
                        arguments={"command": first_command, "wait_ms": 0},
                    )
                )
            )
            assert first["status"] == "running"
            await _wait_for_file_text(order, "first-start\n")

            second = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_second",
                        name="exec",
                        arguments={"command": second_command, "wait_ms": 10},
                    )
                )
            )
            third = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_third",
                        name="exec",
                        arguments={"command": third_command, "wait_ms": 10},
                    )
                )
            )

            assert second["status"] == "queued"
            assert second["is_terminal"] is False
            assert third["status"] == "queued"
            assert third["is_terminal"] is False
            assert order.read_text() == "first-start\n"

            release.touch()
            first_terminal = await _read_until_terminal(
                binding,
                str(first["exec_id"]),
                cursor=0,
            )
            second_terminal = await _read_until_terminal(
                binding,
                str(second["exec_id"]),
                cursor=0,
            )
            third_terminal = await _read_until_terminal(
                binding,
                str(third["exec_id"]),
                cursor=0,
            )

            assert first_terminal["status"] == "exited"
            assert second_terminal["status"] == "exited"
            assert third_terminal["status"] == "exited"
            assert order.read_text() == ("first-start\nfirst-end\nsecond\nthird\n")
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_promotes_next_shell_execution_after_process_start_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    real_create_subprocess_shell = asyncio.create_subprocess_shell
    first_spawn_started: asyncio.Event
    release_first_spawn: asyncio.Event
    spawn_count = 0

    async def controlled_create_subprocess_shell(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            first_spawn_started.set()
            await release_first_spawn.wait()
            raise OSError("synthetic process start failure")
        return await real_create_subprocess_shell(*args, **kwargs)

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_shell",
        controlled_create_subprocess_shell,
    )

    async def scenario() -> None:
        nonlocal first_spawn_started, release_first_spawn
        first_spawn_started = asyncio.Event()
        release_first_spawn = asyncio.Event()
        proof = tmp_path / "promoted-after-failure"
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel
        try:
            first = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_start_failure",
                        name="exec",
                        arguments={"command": "does-not-start", "wait_ms": 0},
                    )
                )
            )
            await first_spawn_started.wait()

            second = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_after_failure",
                        name="exec",
                        arguments={
                            "command": _python_command(
                                "from pathlib import Path; "
                                "Path('promoted-after-failure').touch()"
                            ),
                            "wait_ms": 10,
                        },
                    )
                )
            )
            assert second["status"] == "queued"
            assert not proof.exists()

            release_first_spawn.set()
            first_terminal = await _read_until_terminal(
                binding,
                str(first["exec_id"]),
                cursor=0,
            )
            second_terminal = await _read_until_terminal(
                binding,
                str(second["exec_id"]),
                cursor=0,
            )

            assert first_terminal["status"] == "failed"
            assert first_terminal["exit_code"] is None
            assert second_terminal["status"] == "exited"
            assert proof.exists()
        finally:
            release_first_spawn.set()
            await kernel.close()

    asyncio.run(scenario())


def test_defaults_to_thirty_two_pending_executions(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = tmp_path / "default-capacity-started"
        release = tmp_path / "default-capacity-release"
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel
        try:
            running = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_default_running",
                        name="exec",
                        arguments={
                            "command": _blocking_command(started, release),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            assert running["status"] == "running"
            await _wait_for_path(started)

            for index in range(32):
                queued = _output(
                    await binding.dispatch(
                        ToolCall(
                            call_id=f"exec_default_queued_{index}",
                            name="exec",
                            arguments={
                                "command": _python_command("pass"),
                                "wait_ms": 0,
                            },
                        )
                    )
                )
                assert queued["status"] == "queued"

            overflow = await binding.dispatch(
                ToolCall(
                    call_id="exec_default_overflow",
                    name="exec",
                    arguments={
                        "command": _python_command("pass"),
                        "wait_ms": 0,
                    },
                )
            )
            assert _error(overflow) == {
                "ok": False,
                "code": "queue_full",
                "message": "execution pending queue is full",
            }
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_configured_pending_capacity_releases_on_promotion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_started = tmp_path / "configured-first-started"
        first_release = tmp_path / "configured-first-release"
        second_started = tmp_path / "configured-second-started"
        second_release = tmp_path / "configured-second-release"
        kernel = EnvironmentKernel(tmp_path, queue_limit=1)
        binding = kernel
        try:
            first = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_configured_first",
                        name="exec",
                        arguments={
                            "command": _blocking_command(
                                first_started,
                                first_release,
                            ),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(first_started)
            second = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_configured_second",
                        name="exec",
                        arguments={
                            "command": _blocking_command(
                                second_started,
                                second_release,
                            ),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            assert first["status"] == "running"
            assert second["status"] == "queued"

            overflow = await binding.dispatch(
                ToolCall(
                    call_id="exec_configured_overflow",
                    name="exec",
                    arguments={
                        "command": _python_command("pass"),
                        "wait_ms": 0,
                    },
                )
            )
            assert _error(overflow)["code"] == "queue_full"
            assert len(kernel._executions) == 2

            first_release.touch()
            await _wait_for_path(second_started)
            first_terminal = await _read_until_terminal(
                binding,
                str(first["exec_id"]),
                cursor=0,
            )
            assert first_terminal["status"] == "exited"

            admitted_after_promotion = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_after_promotion",
                        name="exec",
                        arguments={
                            "command": _python_command(
                                "from pathlib import Path; "
                                "Path('ran-after-promotion').touch()"
                            ),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            assert admitted_after_promotion["status"] == "queued"
            assert len(kernel._executions) == 3

            second_release.touch()
            second_terminal = await _read_until_terminal(
                binding,
                str(second["exec_id"]),
                cursor=0,
            )
            promoted_terminal = await _read_until_terminal(
                binding,
                str(admitted_after_promotion["exec_id"]),
                cursor=0,
            )
            assert second_terminal["status"] == "exited"
            assert promoted_terminal["status"] == "exited"
            assert (tmp_path / "ran-after-promotion").exists()
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_policy_denial_bypasses_saturated_queue_and_new_session_is_fresh(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = tmp_path / "denial-capacity-started"
        release = tmp_path / "denial-capacity-release"
        kernel = EnvironmentKernel(
            tmp_path,
            queue_limit=1,
            policy=_DenyExecutablePolicy(
                frozenset({"rm"}),
                reason="direct invocation of 'rm' is denied by policy",
            ),
        )
        fresh_kernel: EnvironmentKernel | None = None
        binding = kernel
        try:
            await binding.dispatch(
                ToolCall(
                    call_id="exec_denial_running",
                    name="exec",
                    arguments={
                        "command": _blocking_command(started, release),
                        "wait_ms": 0,
                    },
                )
            )
            await _wait_for_path(started)
            queued = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_denial_queued",
                        name="exec",
                        arguments={
                            "command": _python_command("pass"),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            assert queued["status"] == "queued"

            denied = await binding.dispatch(
                ToolCall(
                    call_id="exec_denied_while_full",
                    name="exec",
                    arguments={"command": "rm proof.txt", "wait_ms": 0},
                )
            )
            assert _error(denied) == {
                "ok": False,
                "code": "policy_denied",
                "message": "direct invocation of 'rm' is denied by policy",
            }
            assert len(kernel._executions) == 2

            await binding.close()
            fresh_kernel = EnvironmentKernel(
                tmp_path,
                queue_limit=1,
            )
            fresh = _output(
                await fresh_kernel.dispatch(
                    ToolCall(
                        call_id="exec_fresh_capacity",
                        name="exec",
                        arguments={"command": _python_command("pass")},
                    )
                )
            )
            assert fresh["status"] == "exited"
        finally:
            await kernel.close()
            if fresh_kernel is not None:
                await fresh_kernel.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cancel_index",
    (0, 1, 2),
    ids=("first", "middle", "last"),
)
def test_kill_removes_queued_execution_and_reuses_capacity(
    tmp_path: Path,
    cancel_index: int,
) -> None:
    async def scenario() -> None:
        started = tmp_path / f"cancel-{cancel_index}-started"
        release = tmp_path / f"cancel-{cancel_index}-release"
        order = tmp_path / f"cancel-{cancel_index}-order"
        labels = ("first", "middle", "last")
        kernel = EnvironmentKernel(tmp_path, queue_limit=3)
        binding = kernel
        try:
            running = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id=f"exec_running_{cancel_index}",
                        name="exec",
                        arguments={
                            "command": _blocking_command(started, release),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            await _wait_for_path(started)

            queued = []
            for label in labels:
                queued.append(
                    _output(
                        await binding.dispatch(
                            ToolCall(
                                call_id=f"exec_queued_{cancel_index}_{label}",
                                name="exec",
                                arguments={
                                    "command": _append_order_command(order, label),
                                    "wait_ms": 0,
                                },
                            )
                        )
                    )
                )
            assert [snapshot["status"] for snapshot in queued] == [
                "queued",
                "queued",
                "queued",
            ]

            selected = queued[cancel_index]
            killed = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id=f"kill_queued_{cancel_index}",
                        name="kill",
                        arguments={"exec_id": selected["exec_id"]},
                    )
                )
            )
            killed_again = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id=f"kill_queued_again_{cancel_index}",
                        name="kill",
                        arguments={"exec_id": selected["exec_id"]},
                    )
                )
            )
            reread = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id=f"output_killed_{cancel_index}",
                        name="output",
                        arguments={"exec_id": selected["exec_id"]},
                    )
                )
            )

            assert killed["status"] == "killed"
            assert killed["is_terminal"] is True
            assert killed["exit_code"] is None
            assert killed["chunks"] == []
            assert killed["next_cursor"] == 0
            assert killed_again == killed
            assert reread == killed

            killed_state = kernel._executions[str(selected["exec_id"])]
            assert killed_state.kill_requested is True
            assert killed_state.prepared_execution is None
            assert killed_state.completion_task is None

            replacement = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id=f"exec_replacement_{cancel_index}",
                        name="exec",
                        arguments={
                            "command": _append_order_command(order, "replacement"),
                            "wait_ms": 0,
                        },
                    )
                )
            )
            assert replacement["status"] == "queued"

            release.touch()
            assert (
                await _read_until_terminal(
                    binding,
                    str(running["exec_id"]),
                    cursor=0,
                )
            )["status"] == "exited"
            for index, snapshot in enumerate(queued):
                if index != cancel_index:
                    assert (
                        await _read_until_terminal(
                            binding,
                            str(snapshot["exec_id"]),
                            cursor=0,
                        )
                    )["status"] == "exited"
            assert (
                await _read_until_terminal(
                    binding,
                    str(replacement["exec_id"]),
                    cursor=0,
                )
            )["status"] == "exited"

            expected_order = [
                label for index, label in enumerate(labels) if index != cancel_index
            ]
            expected_order.append("replacement")
            assert order.read_text().splitlines() == expected_order
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_killing_queued_execution_wakes_exec_and_output_waiters(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = tmp_path / "waiter-running-started"
        release = tmp_path / "waiter-running-release"
        must_not_start = tmp_path / "queued-must-not-start"
        kernel = EnvironmentKernel(tmp_path, queue_limit=1)
        binding = kernel
        try:
            await binding.dispatch(
                ToolCall(
                    call_id="exec_waiter_running",
                    name="exec",
                    arguments={
                        "command": _blocking_command(started, release),
                        "wait_ms": 0,
                    },
                )
            )
            await _wait_for_path(started)

            exec_waiter = asyncio.create_task(
                binding.dispatch(
                    ToolCall(
                        call_id="exec_waiting_queued",
                        name="exec",
                        arguments={
                            "command": _python_command(
                                "from pathlib import Path; "
                                "Path('queued-must-not-start').touch()"
                            ),
                            "wait_ms": 5_000,
                        },
                    )
                )
            )
            queued_state = await _wait_for_queued_state(kernel)
            output_waiter = asyncio.create_task(
                binding.dispatch(
                    ToolCall(
                        call_id="output_waiting_queued",
                        name="output",
                        arguments={
                            "exec_id": queued_state.exec_id,
                            "wait_ms": 5_000,
                        },
                    )
                )
            )
            await asyncio.sleep(0)
            assert not exec_waiter.done()
            assert not output_waiter.done()

            killed = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="kill_waiting_queued",
                        name="kill",
                        arguments={"exec_id": queued_state.exec_id},
                    )
                )
            )
            exec_result = _output(await asyncio.wait_for(exec_waiter, timeout=0.5))
            output_result = _output(await asyncio.wait_for(output_waiter, timeout=0.5))

            assert killed["status"] == "killed"
            assert exec_result == killed
            assert output_result == killed
            assert queued_state.prepared_execution is None
            assert queued_state.completion_task is None
            assert not must_not_start.exists()
        finally:
            release.touch()
            await kernel.close()

    asyncio.run(scenario())


def test_pending_kill_and_promotion_have_one_atomic_winner() -> None:
    command = parse_shell_ast("true")

    cancel_wins = _ExecutionScheduler(queue_limit=1)
    running_admission = cancel_wins.admit(
        command,
        _router().resolve(command),
    )
    queued_admission = cancel_wins.admit(
        command,
        _router().resolve(command),
    )
    assert running_admission is not None
    assert queued_admission is not None
    running = running_admission.state
    queued = queued_admission.state

    assert cancel_wins.cancel_pending(queued) is True
    running.status = "exited"
    assert cancel_wins.complete(running) == ()
    assert queued.status == "killed"
    assert queued.kill_requested is True

    promotion_wins = _ExecutionScheduler(queue_limit=1)
    running_admission = promotion_wins.admit(
        command,
        _router().resolve(command),
    )
    queued_admission = promotion_wins.admit(
        command,
        _router().resolve(command),
    )
    assert running_admission is not None
    assert queued_admission is not None
    running = running_admission.state
    queued = queued_admission.state

    running.status = "exited"
    assert promotion_wins.complete(running) == (queued,)
    assert queued.status == "running"
    assert promotion_wins.cancel_pending(queued) is False
    assert queued.kill_requested is False


def test_output_bound_discards_later_chunks_and_preserves_first_cursor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path, chunk_limit=1)
        binding = kernel
        try:
            started = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_truncated",
                        name="exec",
                        arguments={
                            "command": _python_command(
                                "import time; "
                                "print('first', flush=True); "
                                "time.sleep(0.1); "
                                "print('second', flush=True)"
                            ),
                            "wait_ms": 10,
                        },
                    )
                )
            )
            first = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="output_retained",
                        name="output",
                        arguments={
                            "exec_id": started["exec_id"],
                            "cursor": 0,
                            "wait_ms": 500,
                        },
                    )
                )
            )
            terminal = await _read_until_terminal(
                binding,
                str(started["exec_id"]),
                cursor=int(first["next_cursor"]),
            )
            reread = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="output_reread",
                        name="output",
                        arguments={"exec_id": started["exec_id"], "cursor": 0},
                    )
                )
            )

            assert _stream_text(first["chunks"], "stdout") == "first\n"
            assert terminal["chunks"] == []
            assert terminal["truncated"] is True
            assert terminal["available_from"] == 0
            assert reread["chunks"] == first["chunks"]
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_kill_terminates_shell_process_group_and_descendant(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker = tmp_path / "descendant-finished"
        child = (
            "import time; from pathlib import Path; "
            f"time.sleep(0.4); Path({str(marker)!r}).write_text('leaked')"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "print('ready', flush=True); "
            "time.sleep(10)"
        )
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel
        try:
            started = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_group",
                        name="exec",
                        arguments={
                            "command": _python_command(parent),
                            "wait_ms": 20,
                        },
                    )
                )
            )
            ready = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="output_ready",
                        name="output",
                        arguments={
                            "exec_id": started["exec_id"],
                            "wait_ms": 500,
                        },
                    )
                )
            )
            assert "ready\n" in _stream_text(ready["chunks"], "stdout")

            killed = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="kill_group",
                        name="kill",
                        arguments={"exec_id": started["exec_id"]},
                    )
                )
            )

            assert killed["status"] == "killed"
            assert killed["is_terminal"] is True
            await asyncio.sleep(0.5)
            assert not marker.exists()
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_close_terminates_shell_process_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker = tmp_path / "kernel-descendant-finished"
        child = (
            "import time; from pathlib import Path; "
            f"time.sleep(0.4); Path({str(marker)!r}).write_text('leaked')"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "print('ready', flush=True); "
            "time.sleep(10)"
        )
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel
        try:
            started = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="exec_kernel_close",
                        name="exec",
                        arguments={
                            "command": _python_command(parent),
                            "wait_ms": 20,
                        },
                    )
                )
            )
            ready = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id="output_kernel_ready",
                        name="output",
                        arguments={
                            "exec_id": started["exec_id"],
                            "wait_ms": 500,
                        },
                    )
                )
            )
            assert "ready\n" in _stream_text(ready["chunks"], "stdout")

            await kernel.close()

            await asyncio.sleep(0.5)
            assert not marker.exists()
        finally:
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "call",
    (
        ToolCall(call_id="unknown", name="read", arguments={}),
        ToolCall(
            call_id="extra",
            name="exec",
            arguments={"command": "pwd", "cwd": "/"},
        ),
        ToolCall(call_id="missing", name="output", arguments={}),
        ToolCall(
            call_id="cursor",
            name="output",
            arguments={"exec_id": "id", "cursor": -1},
        ),
        ToolCall(
            call_id="limit",
            name="kill",
            arguments={"exec_id": "id", "limit": 0},
        ),
    ),
)
def test_returns_structured_invalid_argument_errors(
    tmp_path: Path,
    call: ToolCall,
) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(tmp_path)

        result = await binding.dispatch(call)

        error = _error(result)
        assert result.call_id == call.call_id
        assert result.output is None
        assert error["ok"] is False
        assert error["code"] == "invalid_argument"
        assert isinstance(error["message"], str)
        assert error["message"]
        json.dumps(error)

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ("output", "kill"))
def test_rejects_unknown_execution_ids(tmp_path: Path, name: str) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(tmp_path)

        result = await binding.dispatch(
            ToolCall(
                call_id=f"{name}_missing",
                name=name,
                arguments={"exec_id": "does-not-exist"},
            )
        )

        assert _error(result) == {
            "ok": False,
            "code": "unknown_execution",
            "message": "execution not found",
        }

    asyncio.run(scenario())


def test_closes_kernel_idempotently(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)

        await kernel.close()
        await kernel.close()
        closed_result = await kernel.dispatch(
            ToolCall(call_id="closed_kernel", name="exec", arguments={"command": "pwd"})
        )
        assert _error(closed_result)["code"] == "internal"

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


def _append_order_command(order: Path, label: str) -> str:
    line = f"{label}\n"
    return _python_command(
        "from pathlib import Path\n"
        f"order = Path({str(order)!r})\n"
        "with order.open('a') as stream:\n"
        f"    stream.write({line!r})"
    )


def _output(result: ToolResult) -> dict[str, object]:
    assert isinstance(result.output, dict)
    return result.output


def _error(result: ToolResult) -> dict[str, object]:
    assert isinstance(result.error, dict)
    return result.error


def _stream_text(chunks: list[object], stream: str) -> str:
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == stream
    )


async def _read_until_terminal(
    binding: EnvironmentKernel,
    exec_id: str,
    *,
    cursor: int,
) -> dict[str, object]:
    chunks: list[object] = []
    current_cursor = cursor
    for index in range(10):
        result = await binding.dispatch(
            ToolCall(
                call_id=f"output_terminal_{index}",
                name="output",
                arguments={
                    "exec_id": exec_id,
                    "cursor": current_cursor,
                    "wait_ms": 500,
                },
            )
        )
        snapshot = _output(result)
        chunks.extend(snapshot["chunks"])
        current_cursor = int(snapshot["next_cursor"])
        if snapshot["is_terminal"]:
            snapshot["chunks"] = chunks
            return snapshot
    raise AssertionError("execution did not reach a terminal state")


async def _wait_for_file_text(path: Path, expected: str) -> None:
    for _ in range(100):
        if path.exists() and path.read_text() == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} did not contain {expected!r}")


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
