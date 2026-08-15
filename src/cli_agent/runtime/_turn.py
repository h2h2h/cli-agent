"""Runtime-owned turn event streams."""

from __future__ import annotations

import asyncio
from asyncio import Queue
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from cli_agent.runtime.model import ModelEvent


@dataclass(frozen=True, slots=True)
class _TurnEvent:
    event: ModelEvent


@dataclass(frozen=True, slots=True)
class _TurnTerminal:
    error: BaseException | None


class TurnStream:
    """Consume events produced by one Runtime-owned turn task.

    The stream deliberately owns no AgentLoop work. Closing it only asks the
    Runtime to cancel and join the producer; this keeps a consumer task from
    becoming the owner of a live turn.
    """

    def __init__(
        self,
        *,
        queue: Queue[_TurnEvent | _TurnTerminal],
        on_close: Callable[[TurnStream], Awaitable[None]],
        on_finish: Callable[[TurnStream], Awaitable[None]],
    ) -> None:
        self._queue = queue
        self._on_close = on_close
        self._on_finish = on_finish
        self._closed = False
        self._terminal_queued = False
        self._terminal_seen = False
        self._consumer_task: asyncio.Task[object] | None = None

    def __aiter__(self) -> AsyncIterator[ModelEvent]:
        """Return a lightweight iterator that closes abandoned streams."""

        return _TurnConsumer(self)

    async def __anext__(self) -> ModelEvent:
        """Wait for one event or the producer's terminal outcome."""

        if self._closed:
            raise StopAsyncIteration
        current = asyncio.current_task()
        if self._consumer_task is None:
            self._consumer_task = current
        elif self._consumer_task is not current:
            raise RuntimeError("a TurnStream can only have one consumer")
        try:
            item = await self._queue.get()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        if isinstance(item, _TurnEvent):
            return item.event

        self._terminal_seen = True
        self._closed = True
        await self._on_finish(self)
        if item.error is not None:
            raise item.error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        """Stop a producer when the consumer leaves before terminal state."""

        if self._closed:
            return
        self._closed = True
        await self._on_close(self)

    async def _put_event(self, event: ModelEvent) -> None:
        await self._queue.put(_TurnEvent(event))

    def _put_terminal(
        self,
        error: BaseException | None,
        *,
        force: bool = False,
    ) -> None:
        """Publish one terminal outcome, optionally replacing queued events."""

        if self._terminal_queued and not force:
            return
        if force:
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._terminal_queued = False
        self._queue.put_nowait(_TurnTerminal(error))
        self._terminal_queued = True


class _TurnConsumer:
    """Async-for adapter whose lifetime represents one stream consumer."""

    def __init__(self, stream: TurnStream) -> None:
        self._stream = stream

    def __aiter__(self) -> _TurnConsumer:
        return self

    async def __anext__(self) -> ModelEvent:
        return await self._stream.__anext__()

    def __del__(self) -> None:
        if self._stream._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._stream.aclose())


__all__ = ("TurnStream",)
