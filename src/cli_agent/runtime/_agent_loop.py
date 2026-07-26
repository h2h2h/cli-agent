"""Session-scoped model interaction loop."""

from __future__ import annotations

from collections.abc import AsyncIterator

from cli_agent.runtime._environment import EnvironmentBinding
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
        environment: EnvironmentBinding,
        *,
        system_message: SystemMessage,
    ) -> None:
        self._provider = provider
        self._environment = environment
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

            results = []
            for tool_call in tool_calls:
                results.append(await self._environment.dispatch(tool_call))
            self._history.append(ToolResultMessage(content=tuple(results)))
