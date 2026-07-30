"""Session-scoped model interaction loop."""

from __future__ import annotations

from collections.abc import AsyncIterator

from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime.model import (
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


class AgentLoop:
    """Run model turns and retain the active conversation history."""

    def __init__(
        self,
        provider: ModelProvider,
        kernel: EnvironmentKernel,
        *,
        system_message: SystemMessage,
    ) -> None:
        self._provider = provider
        self._kernel = kernel
        self._history: list[ModelMessage] = [system_message]

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the active conversation."""

        return tuple(self._history)

    async def run(self, message: UserMessage) -> AsyncIterator[ModelEvent]:
        """Run one model turn, dispatching Tool Calls in message order."""

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
            self._history.append(completion.message)
            if not tool_calls:
                yield completion
                return

            results = await self._kernel.dispatch_batch(tool_calls)
            self._history.append(ToolResultMessage(content=results))
