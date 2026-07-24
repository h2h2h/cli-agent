"""Session-scoped model interaction loop."""

from __future__ import annotations

from collections.abc import AsyncIterator

from runtime.model import (
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    UserMessage,
)


class AgentLoop:
    """Run model turns and retain the active conversation history."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider
        self._history: list[ModelMessage] = []

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the active conversation."""

        return tuple(self._history)

    async def run(self, message: UserMessage) -> AsyncIterator[ModelEvent]:
        """Run one text-only model turn."""

        self._history.append(message)
        request = ModelRequest(messages=self.history)

        async for event in self._provider.generate(request):
            if isinstance(event, ModelCompletion):
                self._history.append(event.message)
                yield event
                return

            yield event
