"""Reusable in-process ExecutionHandles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionOutputSink,
    ExitStatus,
)

_InlineHandler = Callable[[ExecutionOutputSink], Awaitable[ExitStatus]]


class _InlineExecution:
    """Run one cooperative Runtime-local handler as an ExecutionHandle."""

    def __init__(self, handler: _InlineHandler) -> None:
        self._handler = handler
        self._run_started = False
        self._kill_requested = False

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        if self._run_started:
            raise RuntimeError("ExecutionHandle.run called more than once")
        self._run_started = True
        if self._kill_requested:
            return ExitStatus(_KILLED_BEFORE_START)
        return await self._handler(sink)

    async def kill(self) -> None:
        self._kill_requested = True


def _text_execution(text: str, *, success: bool) -> _InlineExecution:
    """Build one inline execution that writes text to a single stream."""

    async def execute(sink: ExecutionOutputSink) -> ExitStatus:
        await sink.write(
            "stdout" if success else "stderr",
            text.encode("utf-8"),
        )
        return ExitStatus(0 if success else 1)

    return _InlineExecution(execute)
