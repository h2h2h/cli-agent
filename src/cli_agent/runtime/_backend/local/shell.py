"""Local Shell subprocess execution and worker spawner helpers.

The subprocess is created only when :meth:`_LocalShellExecution.run` starts;
cancellation before ``run`` never allocates a process. The optional
``mutation`` seam copies up output-redirected capability targets before the
process spawns.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.handlers.base import (
    _ExecutionOutcome,
    _ExecutionOutput,
)

if TYPE_CHECKING:
    from cli_agent.runtime._backend.local.view import _LocalCapabilityView

_ProcessSpawner = Callable[[], Awaitable[asyncio.subprocess.Process]]


class _LocalShellExecution:
    """Run one ordinary Shell command inside the Local Backend Workspace."""

    def __init__(
        self,
        command: ShellParseResult,
        cwd: Path,
        environment: Mapping[str, str],
        mutation: _LocalCapabilityView | None,
        input_data: bytes | None = None,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._mutation = mutation
        self._cancel_requested = False
        self._process = _ProcessExecution(
            _shell_spawner(command.raw_command, cwd, environment, input_data),
            input_data=input_data,
        )

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        mutation = self._mutation
        if mutation is None:
            return await self._process.run(output)
        async with mutation.prepare_shell(
            self._command,
            self._cwd,
            cancelled=lambda: self._cancel_requested,
        ) as prepared:
            if not prepared:
                return _ExecutionOutcome.killed()
            return await self._process.run(output)

    async def cancel(self) -> None:
        self._cancel_requested = True
        await self._process.cancel()


class _ProcessExecution:
    """Own one child process and its process group."""

    def __init__(
        self,
        spawn: _ProcessSpawner,
        *,
        input_data: bytes | None = None,
    ) -> None:
        self._spawn = spawn
        self._input_data = input_data
        self._process: asyncio.subprocess.Process | None = None
        self._ready = asyncio.Event()
        self._completed = asyncio.Event()
        self._run_started = False
        self._cancel_requested = False

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        self._run_started = True
        process: asyncio.subprocess.Process | None = None
        stdin_task: asyncio.Task[None] | None = None
        try:
            if self._cancel_requested:
                return _ExecutionOutcome.killed()

            process = await self._spawn()
            self._process = process
            self._ready.set()
            if self._cancel_requested:
                _signal_process(process, force=False)
            if self._input_data is not None:
                if process.stdin is None:
                    raise RuntimeError("process input was configured without stdin")
                stdin_task = asyncio.create_task(
                    self._feed_stdin(process.stdin, self._input_data)
                )

            stdout_task = asyncio.create_task(
                self._capture_stream(output, process.stdout, "stdout")
            )
            stderr_task = asyncio.create_task(
                self._capture_stream(output, process.stderr, "stderr")
            )
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            await self._finish_stdin(stdin_task)
            if self._cancel_requested:
                return _ExecutionOutcome.killed(exit_code)
            if exit_code == 0:
                return _ExecutionOutcome.exited(exit_code)
            return _ExecutionOutcome.failed(exit_code)
        except asyncio.CancelledError:
            await self._finish_stdin(stdin_task)
            if process is not None:
                _signal_process(process, force=True)
                with suppress(Exception):
                    await process.wait()
            raise
        except Exception:
            await self._finish_stdin(stdin_task)
            if process is not None:
                _signal_process(process, force=True)
                with suppress(Exception):
                    await process.wait()
            exit_code = process.returncode if process is not None else None
            if self._cancel_requested:
                return _ExecutionOutcome.killed(exit_code)
            return _ExecutionOutcome.failed(exit_code)
        finally:
            self._ready.set()
            self._completed.set()

    async def cancel(self) -> None:
        self._cancel_requested = True
        if not self._run_started:
            return
        await self._ready.wait()
        process = self._process
        if process is None or process.returncode is not None:
            return

        _signal_process(process, force=False)
        try:
            await asyncio.wait_for(
                self._completed.wait(),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            _signal_process(process, force=True)

    async def _feed_stdin(
        self,
        stdin: asyncio.StreamWriter,
        data: bytes,
    ) -> None:
        try:
            stdin.write(data)
            await stdin.drain()
        except ConnectionError:
            pass
        finally:
            stdin.close()

    async def _finish_stdin(self, task: asyncio.Task[None] | None) -> None:
        """Stop and reap one stdin writer without leaking it or the subprocess."""
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def _capture_stream(
        self,
        output: _ExecutionOutput,
        stream: asyncio.StreamReader | None,
        stream_name: Literal["stdout", "stderr"],
    ) -> None:
        if stream is None:
            return
        while data := await stream.read(4096):
            await output.write(stream_name, data)


def _shell_spawner(
    raw_command: str,
    cwd: Path,
    environment: Mapping[str, str],
    input_data: bytes | None = None,
) -> _ProcessSpawner:
    """Return a spawner that starts one Shell process in the Workspace."""

    async def spawn() -> asyncio.subprocess.Process:
        stdin = asyncio.subprocess.PIPE if input_data is not None else None
        return await asyncio.create_subprocess_shell(
            raw_command,
            cwd=cwd,
            env=dict(environment),
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )

    return spawn


def _tool_worker_spawner(
    python: Path,
    worker: Path,
    cwd: Path,
    environment: Mapping[str, str],
) -> _ProcessSpawner:
    """Return a spawner that starts one fresh Tool worker in the Workspace.

    The materialized worker path and the Workspace-private venv Python are
    resolved by the Local Backend; the Handler never sees either path.
    """

    async def spawn() -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            str(python),
            str(worker),
            cwd=cwd,
            env=dict(environment),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )

    return spawn


def _signal_process(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError, PermissionError):
        if os.name == "posix":
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        elif force:
            process.kill()
        else:
            process.terminate()
