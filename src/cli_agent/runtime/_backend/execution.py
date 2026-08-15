"""Generic ExecutionHandles for Backend Filesystem operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionOutputSink,
    ExitStatus,
)

_FilesystemOperation = Callable[[ExecutionOutputSink], Awaitable[ExitStatus]]


class _FilesystemExecution:
    """Run one Workspace Filesystem operation as an ExecutionHandle.

    The operation runs only when :meth:`run` starts; a kill before ``run``
    reports the killed-before-start status without any filesystem side
    effect.
    """

    def __init__(self, operation: _FilesystemOperation) -> None:
        self._operation = operation
        self._run_started = False
        self._kill_requested = False

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        if self._run_started:
            raise RuntimeError("ExecutionHandle.run called more than once")
        self._run_started = True
        if self._kill_requested:
            return ExitStatus(_KILLED_BEFORE_START)
        return await self._operation(sink)

    async def kill(self) -> None:
        self._kill_requested = True
