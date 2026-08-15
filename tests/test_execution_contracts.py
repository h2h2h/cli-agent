"""Shared ExecutionHandle lifecycle contract tests (RFC-0012 issue 011).

One contract suite applies to every concrete handle family: inline,
filesystem, shell, and process. The tests pin single-shot ``run``,
three-phase idempotent ``kill``, bytes-only sinks, signal-normalized
exit codes, cancellation cleanup, and the exit-code versus
``BackendExecutionError`` boundary between command semantics and
execution-environment mechanism failures.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal

import pytest

from cli_agent.runtime._backend import _ShellExecutionRequest
from cli_agent.runtime._backend.execution import _FilesystemExecution
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalShellExecution,
    _ProcessExecution,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    BackendExecutionError,
    ExecutionOutputSink,
    ExitStatus,
)


class _Sink:
    def __init__(self) -> None:
        self.chunks: list[tuple[Literal["stdout", "stderr"], bytes]] = []

    async def write(
        self,
        stream: Literal["stdout", "stderr"],
        data: bytes,
    ) -> None:
        self.chunks.append((stream, data))


def _inline_handle(exit_code: int = 0, *, invoke: list[bool] | None = None):
    async def handler(sink: ExecutionOutputSink) -> ExitStatus:
        if invoke is not None:
            invoke.append(True)
        await sink.write("stdout", b"inline\n")
        return ExitStatus(exit_code)

    return _InlineExecution(handler)


def _filesystem_handle(tmp_path: Path, *, writes: list[Path] | None = None):
    target = tmp_path / "contract.txt"

    async def operation(sink: ExecutionOutputSink) -> ExitStatus:
        if writes is not None:
            writes.append(target)
        target.write_bytes(b"written\n")
        await sink.write("stdout", b"wrote\n")
        return ExitStatus(0)

    return _FilesystemExecution(operation)


def _shell_handle(tmp_path: Path, command: str, *, input_data: bytes | None = None):
    return _LocalBackendWorkspace(tmp_path, {}).prepare_shell(
        _shell_request(command, tmp_path, input_data=input_data)
    )


def _shell_request(
    command: str,
    cwd: Path,
    *,
    input_data: bytes | None = None,
):
    return _ShellExecutionRequest(
        command=parse_shell_ast(command),
        cwd=str(cwd),
        environment={},
        input_data=input_data,
    )


def _process_handle(
    code: str,
    *,
    spawn_fails: bool = False,
):
    if spawn_fails:

        async def spawn() -> asyncio.subprocess.Process:
            raise OSError("worker spawn failed")

    else:

        async def spawn() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

    return _ProcessExecution(spawn)


async def _double_run(handle) -> ExitStatus:
    sink = _Sink()
    await handle.run(sink)
    return await handle.run(_Sink())


@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp_path: _inline_handle(),
        lambda tmp_path: _filesystem_handle(tmp_path),
        lambda tmp_path: _shell_handle(tmp_path, "echo hi"),
        lambda tmp_path: _process_handle("print('hi')"),
    ],
)
def test_second_run_is_an_invariant_violation(factory, tmp_path: Path) -> None:
    async def scenario() -> None:
        handle = factory(tmp_path)
        with pytest.raises(RuntimeError):
            await _double_run(handle)

    asyncio.run(scenario())


def test_run_returns_exit_status_for_success_and_nonzero(tmp_path: Path) -> None:
    async def scenario() -> None:
        assert await _inline_handle(3).run(_Sink()) == 3
        assert await _shell_handle(tmp_path, "exit 3").run(_Sink()) == 3
        assert await _process_handle("import sys; sys.exit(3)").run(_Sink()) == 3
        assert await _filesystem_handle(tmp_path).run(_Sink()) == 0

    asyncio.run(scenario())


def test_command_not_found_is_exit_127(tmp_path: Path) -> None:
    async def scenario() -> None:
        handle = _shell_handle(tmp_path, "cli_agent_no_such_command_xyz")
        assert await handle.run(_Sink()) == 127

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp_path: _inline_handle(invoke=[]),
        lambda tmp_path: _filesystem_handle(tmp_path, writes=[]),
        lambda tmp_path: _shell_handle(tmp_path, "echo hi"),
        lambda tmp_path: _process_handle("print('hi')"),
    ],
)
def test_kill_before_run_reports_killed_before_start_and_allocates_nothing(
    factory,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        handle = factory(tmp_path)
        await handle.kill()
        await handle.kill()

        assert await handle.run(_Sink()) == _KILLED_BEFORE_START
        if isinstance(handle, _FilesystemExecution):
            assert not (tmp_path / "contract.txt").exists()

    asyncio.run(scenario())


def test_kill_during_run_returns_signal_normalized_exit(tmp_path: Path) -> None:
    async def scenario() -> None:
        for handle in (
            _shell_handle(tmp_path, "sleep 30"),
            _process_handle("import time; time.sleep(30)"),
        ):
            task = asyncio.create_task(handle.run(_Sink()))
            await asyncio.sleep(0.2)
            await handle.kill()
            assert await task == 143

    asyncio.run(scenario())


def test_kill_after_terminal_is_an_idempotent_noop(tmp_path: Path) -> None:
    async def scenario() -> None:
        handle = _shell_handle(tmp_path, "echo done")
        assert await handle.run(_Sink()) == 0
        await handle.kill()
        await handle.kill()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "factory",
    [
        lambda tmp_path: _shell_handle(tmp_path, "sleep 30"),
        lambda tmp_path: _process_handle("import time; time.sleep(30)"),
    ],
)
def test_cancelled_run_releases_owned_process(factory, tmp_path: Path) -> None:
    async def scenario() -> None:
        handle = factory(tmp_path)
        task = asyncio.create_task(handle.run(_Sink()))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)

        assert isinstance(handle, (_LocalShellExecution, _ProcessExecution))
        process = (
            handle._process if isinstance(handle, _LocalShellExecution) else handle
        )
        assert process._process is not None
        assert process._process.returncode is not None

    asyncio.run(scenario())


def test_infrastructure_failure_raises_backend_execution_error() -> None:
    async def scenario() -> None:
        handle = _process_handle("print('never')", spawn_fails=True)
        with pytest.raises(BackendExecutionError):
            await handle.run(_Sink())

    asyncio.run(scenario())


def test_stdin_is_pushed_to_the_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        handle = _shell_handle(
            tmp_path,
            "cat",
            input_data=b"hello stdin\n",
        )
        sink = _Sink()
        assert await handle.run(sink) == 0
        assert ("stdout", b"hello stdin\n") in sink.chunks

    asyncio.run(scenario())


def test_dual_streams_push_only_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        handle = _shell_handle(
            tmp_path,
            "echo out; echo err >&2",
        )
        sink = _Sink()
        assert await handle.run(sink) == 0

        assert {stream for stream, _ in sink.chunks} <= {"stdout", "stderr"}
        for stream, data in sink.chunks:
            assert isinstance(data, bytes)
        assert (
            b"".join(data for stream, data in sink.chunks if stream == "stdout")
            == b"out\n"
        )
        assert (
            b"".join(data for stream, data in sink.chunks if stream == "stderr")
            == b"err\n"
        )

    asyncio.run(scenario())


def test_handles_expose_no_pull_events_api(tmp_path: Path) -> None:
    async def scenario() -> None:
        for handle in (
            _inline_handle(),
            _filesystem_handle(tmp_path),
            _shell_handle(tmp_path, "echo hi"),
            _process_handle("print('hi')"),
        ):
            public = {name for name in dir(handle) if not name.startswith("_")}
            assert {"run", "kill"} <= public
            assert not {"events", "status", "outcome"} & public

    asyncio.run(scenario())
