import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._environment import EnvironmentBinding, EnvironmentKernel
from cli_agent.runtime._environment.policy import (
    CommandAnalysis,
    DirectExecutableDenyPolicy,
    ExecutionPlanCandidate,
)


def test_executes_short_command_and_retains_ordered_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel.create_binding()
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
        binding = EnvironmentKernel(tmp_path).create_binding()

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
def test_denies_recognized_direct_rm_before_execution(
    tmp_path: Path,
    command: str,
) -> None:
    async def scenario() -> None:
        proof = tmp_path / "proof.txt"
        proof.write_text("preserved")
        binding = EnvironmentKernel(tmp_path).create_binding()

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
            "message": "direct invocation of 'rm' is denied by policy",
        }
        assert proof.read_text() == "preserved"

    asyncio.run(scenario())


def test_policy_failure_fails_closed_without_starting_command(tmp_path: Path) -> None:
    class FailingPolicy:
        async def authorize(
            self,
            candidate: ExecutionPlanCandidate,
        ) -> object:
            raise RuntimeError(candidate.command)

    async def scenario() -> None:
        proof = tmp_path / "proof.txt"
        binding = EnvironmentKernel(
            tmp_path,
            execution_policy=FailingPolicy(),
        ).create_binding()

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
            "code": "internal",
            "message": "execution policy failed closed",
        }
        assert not proof.exists()

    asyncio.run(scenario())


def test_direct_guard_documents_wrapper_noncoverage(tmp_path: Path) -> None:
    async def scenario() -> None:
        policy = DirectExecutableDenyPolicy()
        candidate = ExecutionPlanCandidate(
            operation="shell.execute",
            command="env rm proof.txt",
            cwd=tmp_path,
            wait_ms=0,
            output_limit=1,
            analysis=CommandAnalysis(
                executable_basename="env",
                tokenization_succeeded=True,
            ),
        )

        decision = await policy.authorize(candidate)

        assert decision.allowed is True

    asyncio.run(scenario())


def test_wait_timeout_returns_running_execution_and_incremental_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel.create_binding()
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


def test_output_bound_discards_later_chunks_and_preserves_first_cursor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path, output_chunk_bound=1)
        binding = kernel.create_binding()
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
        binding = kernel.create_binding()
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


@pytest.mark.parametrize("close_target", ("session", "runtime"))
def test_close_terminates_shell_process_group(
    tmp_path: Path,
    close_target: str,
) -> None:
    async def scenario() -> None:
        marker = tmp_path / f"{close_target}-descendant-finished"
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
        binding = kernel.create_binding()
        try:
            started = _output(
                await binding.dispatch(
                    ToolCall(
                        call_id=f"exec_{close_target}_close",
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
                        call_id=f"output_{close_target}_ready",
                        name="output",
                        arguments={
                            "exec_id": started["exec_id"],
                            "wait_ms": 500,
                        },
                    )
                )
            )
            assert "ready\n" in _stream_text(ready["chunks"], "stdout")

            if close_target == "session":
                await binding.close()
            else:
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
        ToolCall(call_id="blank", name="exec", arguments={"command": "  "}),
        ToolCall(
            call_id="extra",
            name="exec",
            arguments={"command": "pwd", "cwd": "/"},
        ),
        ToolCall(
            call_id="bool",
            name="exec",
            arguments={"command": "pwd", "wait_ms": True},
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
        binding = EnvironmentKernel(tmp_path).create_binding()

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
        binding = EnvironmentKernel(tmp_path).create_binding()

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


def test_closes_binding_and_kernel_idempotently(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel.create_binding()

        await binding.close()
        await binding.close()
        closed_session_result = await binding.dispatch(
            ToolCall(
                call_id="closed_session", name="exec", arguments={"command": "pwd"}
            )
        )
        assert _error(closed_session_result)["code"] == "internal"

        second_binding = kernel.create_binding()
        await kernel.close()
        await kernel.close()
        await second_binding.close()
        closed_kernel_result = await second_binding.dispatch(
            ToolCall(call_id="closed_kernel", name="exec", arguments={"command": "pwd"})
        )
        assert _error(closed_kernel_result)["code"] == "internal"

        with pytest.raises(RuntimeError, match="EnvironmentKernel is closed"):
            kernel.create_binding()

    asyncio.run(scenario())


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


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
    binding: EnvironmentBinding,
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
