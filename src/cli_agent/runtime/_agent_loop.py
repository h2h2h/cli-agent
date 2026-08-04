"""Session-scoped model interaction loop."""

from __future__ import annotations

from collections.abc import AsyncIterator

from cli_agent.runtime._context import ContextPolicy
from cli_agent.runtime._context_manager import _ContextManager
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime.model import (
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelProvider,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


class AgentLoop:
    """Orchestrate model turns against a Session-scoped Context Manager."""

    def __init__(
        self,
        provider: ModelProvider,
        kernel: EnvironmentKernel,
        *,
        system_message: SystemMessage,
        context_policy: ContextPolicy,
    ) -> None:
        self._provider = provider
        self._kernel = kernel
        self._context = _ContextManager(
            system_message=system_message,
            context_policy=context_policy,
            provider=provider,
        )

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the active conversation."""

        return self._context.history

    async def run(self, message: UserMessage) -> AsyncIterator[ModelEvent]:
        """Run one model turn, dispatching Tool Calls in message order."""

        self._context.append(message)
        while True:
            completion = None
            prepared = await self._context.prepare_request()

            async for event in self._provider.generate(prepared.request):
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
            self._context.observe(prepared.revision, completion.usage)
            self._context.append(completion.message)
            if not tool_calls:
                yield completion
                return

            results = await self._kernel.dispatch_batch(tool_calls)
            self._context.append(ToolResultMessage(content=results))
