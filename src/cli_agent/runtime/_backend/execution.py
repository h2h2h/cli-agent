"""Generic prepared Executions for Backend Filesystem operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cli_agent.runtime._environment.handlers.base import (
    _ExecutionOutcome,
    _ExecutionOutput,
)

_FilesystemOperation = Callable[[_ExecutionOutput], Awaitable[_ExecutionOutcome]]


class _FilesystemExecution:
    """Run one Workspace Filesystem operation inside the Execution lifecycle.

    The operation runs only when :meth:`run` starts; cancellation before
    ``run`` produces a killed outcome without any filesystem side effect.
    """

    def __init__(self, operation: _FilesystemOperation) -> None:
        self._operation = operation
        self._cancel_requested = False

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        if self._cancel_requested:
            return _ExecutionOutcome.killed()
        return await self._operation(output)

    async def cancel(self) -> None:
        self._cancel_requested = True
