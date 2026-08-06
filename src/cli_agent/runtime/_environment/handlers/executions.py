"""Reusable in-process prepared Executions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cli_agent.runtime._environment.handlers.base import (
    _ExecutionOutcome,
    _ExecutionOutput,
)

_InlineHandler = Callable[[_ExecutionOutput], Awaitable[_ExecutionOutcome]]


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


def _text_execution(text: str, *, success: bool) -> _InlineExecution:
    """Build one inline execution that writes text to a single stream."""

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        await output.write(
            "stdout" if success else "stderr",
            text.encode("utf-8"),
        )
        return _ExecutionOutcome.exited() if success else _ExecutionOutcome.failed(1)

    return _InlineExecution(execute)
