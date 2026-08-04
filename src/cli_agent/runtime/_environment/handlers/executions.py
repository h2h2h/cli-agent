"""Reusable in-process and subprocess prepared Executions."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Literal

from cli_agent.runtime._environment.handlers.base import (
    _ExecutionOutcome,
    _ExecutionOutput,
)

_InlineHandler = Callable[[_ExecutionOutput], Awaitable[_ExecutionOutcome]]
_ProcessSpawner = Callable[[], Awaitable[asyncio.subprocess.Process]]


class _InlineExecution:
    """Run one cooperative Runtime-local handler."""

    def __init__(self, handler: _InlineHandler) -> None:
        self._handler = handler
        self._cancel_requested = False

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        if self._cancel_requested:
            return _ExecutionOutcome.killed()
        return await self._handler(output)

    async def cancel(self) -> None:
        self._cancel_requested = True


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
                process.stdin.write(self._input_data)
                await process.stdin.drain()
                process.stdin.close()

            stdout_task = asyncio.create_task(
                self._capture_stream(output, process.stdout, "stdout")
            )
            stderr_task = asyncio.create_task(
                self._capture_stream(output, process.stderr, "stderr")
            )
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            if self._cancel_requested:
                return _ExecutionOutcome.killed(exit_code)
            if exit_code == 0:
                return _ExecutionOutcome.exited(exit_code)
            return _ExecutionOutcome.failed(exit_code)
        except Exception:
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


def _signal_process(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        elif force:
            process.kill()
        else:
            process.terminate()


def _text_execution(text: str, *, success: bool) -> _InlineExecution:
    """Build one inline execution that writes text to a single stream."""

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        await output.write(
            "stdout" if success else "stderr",
            text.encode("utf-8"),
        )
        return _ExecutionOutcome.exited() if success else _ExecutionOutcome.failed(1)

    return _InlineExecution(execute)
