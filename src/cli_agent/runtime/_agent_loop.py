"""Session-scoped model interaction loop."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timezone
from uuid import uuid4

from cli_agent.errors.context import ContextExhaustedError
from cli_agent.runtime._context import ContextEngine, SessionUsage
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._session import ModelCallUsage
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.host import NULL_EVENTS, EventSink, emit_event
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelCompletion,
    ModelContextOverflowSignal,
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
                    lines.append(
                        f"       {json.dumps(block.arguments, sort_keys=True)}"
                    )
        elif isinstance(message, ToolResultMessage):
            for result in message.content:
                body = (
                    result.output if result.error is None else {"error": result.error}
                )
                lines.append(f"    <- {result.call_id}")
                lines.append(f"       {json.dumps(body, sort_keys=True)}")
    lines.append("=" * 60)
    return "\n".join(lines)


class AgentLoop:
    """Orchestrate model turns against a Session-scoped ContextEngine.

    The loop never persists conversation state itself: ``commit``
    durably appends one message through the SessionStore and returns the
    new journal revision, and only then is the message applied to the
    ContextEngine, so the in-memory projection can never run ahead of
    the canonical journal.
    """

    def __init__(
        self,
        provider: ModelProvider,
        kernel: EnvironmentKernel,
        *,
        context: ContextEngine,
        commit: Callable[[ModelMessage], int],
        commit_completion: Callable[
            [AssistantMessage, ModelCallUsage | None], int
        ] | None = None,
        events: EventSink = NULL_EVENTS,
    ) -> None:
        self._provider = provider
        self._kernel = kernel
        self._context = context
        self._commit = commit
        self._commit_completion = commit_completion
        self._session_id = context.session_id
        self._events = events

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the active conversation."""

        return self._context.history

    @property
    def usage(self) -> SessionUsage:
        """Return the session-cumulative token usage snapshot."""

        return self._context.usage

    def close(self) -> None:
        """Release the bound ContextEngine resources."""

        self._context.close()

    async def run(self, message: UserMessage) -> AsyncIterator[ModelEvent]:
        """Run one model turn, dispatching Tool Calls in message order."""

        self._context.apply(message, self._commit(message))
        while True:
            await self._kernel.reconcile_library()
            completion = None
            prepared = await self._context.prepare()
            request = prepared.request
            retried = False
            while True:
                model_call_id = uuid4().hex
                try:
                    async for event in self._provider.generate(request):
                        if isinstance(event, ModelCompletion):
                            completion = event
                            break

                        yield event
                    break
                except ModelContextOverflowSignal as exc:
                    if retried:
                        raise ContextExhaustedError(
                            session_id=self._session_id,
                            projected_input_tokens=(
                                prepared.pressure.projected_input_tokens
                            ),
                            input_budget=prepared.pressure.input_budget,
                        ) from exc
                    retried = True
                    recovered = await self._context.force_prepare()
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
            usage = None
            if completion.usage is not None:
                usage = ModelCallUsage(
                    model_call_id=model_call_id,
                    session_id=self._session_id,
                    purpose="agent",
                    input_tokens=completion.usage.input_tokens,
                    output_tokens=completion.usage.output_tokens,
                    created_at=datetime.now(timezone.utc),
                )
            if self._commit_completion is None:
                revision = self._commit(completion.message)
            else:
                revision = self._commit_completion(completion.message, usage)
            self._context.observe_usage(completion.usage)
            self._context.apply(completion.message, revision)
            if not tool_calls:
                self._print_history()
                yield completion
                return

            results = await self._kernel.dispatch(tool_calls)
            self._context.apply(
                ToolResultMessage(content=results),
                self._commit(ToolResultMessage(content=results)),
            )
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
        emit_event(
            self._events,
            RuntimeDiagnostic(
                kind=kind,
                message=message,
                detail=detail or {},
            ),
        )
