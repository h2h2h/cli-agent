"""Session-scoped model interaction loop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping

from cli_agent.runtime._context import ContextPolicy
from cli_agent.runtime._context_manager import (
    ContextOverflowError,
    _ContextManager,
)
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import (
    ModelCompletion,
    ModelContextOverflowError,
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
        session_id: str,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> None:
        self._provider = provider
        self._kernel = kernel
        self._session_id = session_id
        self._on_diagnostic = on_diagnostic
        self._context = _ContextManager(
            system_message=system_message,
            context_policy=context_policy,
            provider=provider,
            session_id=session_id,
            on_diagnostic=on_diagnostic,
        )

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the active conversation."""

        return self._context.history

    async def run(self, message: UserMessage) -> AsyncIterator[ModelEvent]:
        """Run one model turn, dispatching Tool Calls in message order."""

        self._context.append(message)
        while True:
            await self._kernel.reconcile_library()
            completion = None
            prepared = await self._context.prepare_request()
            request = prepared.request
            retried = False
            while True:
                try:
                    async for event in self._provider.generate(request):
                        if isinstance(event, ModelCompletion):
                            completion = event
                            break

                        yield event
                    break
                except ModelContextOverflowError as exc:
                    if retried:
                        raise
                    retried = True
                    recovered = await self._context.force_prepare()
                    if recovered is None:
                        raise ContextOverflowError(
                            "context overflow cannot be recovered safely"
                        ) from exc
                    request = recovered.request
                    prepared = recovered
                    self._emit_diagnostic(
                        "context.overflow_recovery",
                        (
                            "context overflow recovered; retrying the model "
                            f"step at revision {recovered.revision}"
                        ),
                        detail={
                            "session_id": self._session_id,
                            "revision": recovered.revision,
                            "projected_input_tokens": (
                                recovered.pressure.projected_input_tokens
                            ),
                            "input_budget": recovered.pressure.input_budget,
                        },
                    )

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

    def _emit_diagnostic(
        self,
        kind: str,
        message: str,
        *,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        if self._on_diagnostic is None:
            return
        self._on_diagnostic(
            RuntimeDiagnostic(
                kind=kind,
                message=message,
                detail=detail or {},
            )
        )
