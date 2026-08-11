"""Session-scoped model interaction loop."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Mapping

from cli_agent.runtime._context import ContextPolicy
from cli_agent.runtime._context_manager import (
    ContextOverflowError,
    SessionUsage,
    _ContextManager,
)
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelCompletion,
    ModelContextOverflowError,
    ModelEvent,
    ModelMessage,
    ModelProvider,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

PRINT_HISTORY_ENV = "CLI_AGENT_PRINT_HISTORY"


def _render_history(history: tuple[ModelMessage, ...]) -> str:
    """Render the conversation history as one human-readable block."""

    lines = ["=" * 60, f"HISTORY ({len(history)} messages)", "-" * 60]
    for index, message in enumerate(history):
        role = re.sub(
            r"(?<!^)(?=[A-Z])", " ", type(message).__name__.removesuffix("Message")
        ).upper()
        lines.append(f"[{index + 1}] {role}")
        if isinstance(message, (SystemMessage, UserMessage)):
            for block in message.content:
                lines.append(f"    {block.text}")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    lines.append(f"    {block.text}")
                else:
                    lines.append(f"    -> {block.name} [{block.call_id}]")
                    lines.append(f"       {json.dumps(block.arguments, sort_keys=True)}")
        elif isinstance(message, ToolResultMessage):
            for result in message.content:
                body = (
                    result.output
                    if result.error is None
                    else {"error": result.error}
                )
                lines.append(f"    <- {result.call_id}")
                lines.append(f"       {json.dumps(body, sort_keys=True)}")
    lines.append("=" * 60)
    return "\n".join(lines)


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
        on_append: Callable[[ModelMessage], None] | None = None,
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
            on_append=on_append,
        )

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the active conversation."""

        return self._context.history

    @property
    def usage(self) -> SessionUsage:
        """Return the session-cumulative token usage snapshot."""

        return self._context.usage

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
                self._print_history()
                yield completion
                return

            results = await self._kernel.dispatch_batch(tool_calls)
            self._context.append(ToolResultMessage(content=results))
            self._print_history()

    def _print_history(self) -> None:
        """Print the current conversation history in a readable form.

        Enabled only when ``CLI_AGENT_PRINT_HISTORY`` is set to exactly ``"1"``.
        The dump goes to stderr so it never pollutes the stdout protocol
        stream that carries model text output.
        """

        if os.environ.get(PRINT_HISTORY_ENV, "") != "1":
            return
        print(_render_history(self.history), file=sys.stderr)

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
