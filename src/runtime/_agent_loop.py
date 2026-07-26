"""Session-scoped model interaction loop."""

from __future__ import annotations

from collections.abc import AsyncIterator

from runtime._environment import EnvironmentBinding
from runtime.model import (
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


class AgentLoop:
    """Run model turns and retain the active conversation history."""

    def __init__(
        self,
        provider: ModelProvider,
        environment: EnvironmentBinding,
    ) -> None:
        self._provider = provider
        self._environment = environment
        self._history: list[ModelMessage] = []

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the active conversation."""

        return tuple(self._history)

    async def run(self, message: UserMessage) -> AsyncIterator[ModelEvent]:
        """Run one model turn through at most one Tool Call per response."""

        self._history.append(message)
        while True:
            completion = None
            request = ModelRequest(messages=self.history)

            async for event in self._provider.generate(request):
                if isinstance(event, ModelCompletion):
                    completion = event
                    break

                yield event

            if completion is None:
                return

            tool_calls = tuple(
                block
                for block in completion.message.content
                if isinstance(block, ToolCall)
            )
            if len(tool_calls) > 1:
                raise RuntimeError(
                    "AgentLoop supports one Tool Call per model response"
                )

            self._history.append(completion.message)
            if not tool_calls:
                yield completion
                return

            result = await self._environment.dispatch(tool_calls[0])
            self._history.append(ToolResultMessage(content=(result,)))
