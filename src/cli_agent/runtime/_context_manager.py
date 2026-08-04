"""Session-scoped Context ownership: ledger, budget, and request preparation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal

from cli_agent.runtime._context import ContextPolicy
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    ModelRequest,
    ModelUsage,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

UsageSource = Literal["reported", "estimated"]


class _ContextLedgerError(RuntimeError):
    """Raised when a Context History mutation breaks the model protocol."""


@dataclass(frozen=True, slots=True)
class ContextPressure:
    """Token pressure of the next projected normal Model Request."""

    input_budget: int
    projected_input_tokens: int
    usage_source: UsageSource

    @property
    def ratio(self) -> float:
        """Return projected input tokens relative to the Input Budget."""

        return self.projected_input_tokens / self.input_budget


@dataclass(frozen=True, slots=True)
class PreparedContext:
    """Immutable projection of one prepared normal Model Request."""

    request: ModelRequest
    revision: int
    pressure: ContextPressure


class _ContextLedger:
    """Own the ordered conversation and validate Turn and Tool Exchange edges."""

    def __init__(self, system_message: SystemMessage) -> None:
        self._messages: list[ModelMessage] = [system_message]
        self._revision = 0

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return an immutable snapshot of the conversation."""

        return tuple(self._messages)

    @property
    def revision(self) -> int:
        """Return the current Context Revision."""

        return self._revision

    @property
    def message_count(self) -> int:
        """Return the number of messages owned by the Ledger."""

        return len(self._messages)

    def append(self, message: ModelMessage) -> None:
        """Append one message, rejecting protocol-illegal mutations."""

        if isinstance(message, SystemMessage):
            raise _ContextLedgerError("system message cannot be appended")
        if isinstance(message, AssistantMessage):
            self._validate_assistant(message)
        elif isinstance(message, ToolResultMessage):
            self._validate_tool_results(message)
        self._messages.append(message)
        self._revision += 1

    def _validate_assistant(self, message: AssistantMessage) -> None:
        call_ids = [
            block.call_id for block in message.content if isinstance(block, ToolCall)
        ]
        if len(call_ids) != len(set(call_ids)):
            raise _ContextLedgerError(
                "assistant message contains a duplicate tool call_id"
            )
        if isinstance(self._messages[-1], AssistantMessage):
            raise _ContextLedgerError(
                "assistant message must follow a user or tool result message"
            )

    def _validate_tool_results(self, message: ToolResultMessage) -> None:
        previous = self._messages[-1]
        if not isinstance(previous, AssistantMessage):
            raise _ContextLedgerError(
                "tool result appended without a preceding tool call"
            )
        expected = {
            block.call_id for block in previous.content if isinstance(block, ToolCall)
        }
        if not expected:
            raise _ContextLedgerError(
                "tool result appended without a preceding tool call"
            )
        seen: set[str] = set()
        for result in message.content:
            if result.call_id not in expected:
                raise _ContextLedgerError(
                    f"tool result call_id {result.call_id!r} has no matching tool call"
                )
            if result.call_id in seen:
                raise _ContextLedgerError(
                    f"duplicate tool result for call_id {result.call_id!r}"
                )
            seen.add(result.call_id)
        missing = expected - seen
        if missing:
            raise _ContextLedgerError(
                f"tool result is missing call_id {sorted(missing)[0]!r}"
            )


class _ContextManager:
    """Own Context History, revisions, usage anchors, and request preparation."""

    def __init__(
        self,
        *,
        system_message: SystemMessage,
        context_policy: ContextPolicy,
    ) -> None:
        self._ledger = _ContextLedger(system_message)
        self._policy = context_policy
        self._anchor_revision: int | None = None
        self._anchor_message_count = 0
        self._anchor_input_tokens: int | None = None
        self._last_prepared_revision: int | None = None
        self._observed_revision: int | None = None

    @property
    def history(self) -> tuple[ModelMessage, ...]:
        """Return the immutable conversation projection."""

        return self._ledger.history

    @property
    def revision(self) -> int:
        """Return the current Context Revision."""

        return self._ledger.revision

    def append(self, message: ModelMessage) -> None:
        """Append one message; the only write entry to Context History."""

        self._ledger.append(message)

    def observe(self, revision: int, usage: ModelUsage | None) -> None:
        """Anchor Provider-reported usage to one prepared request revision.

        Raises:
            _ContextLedgerError: If the revision was not the last prepared one,
                or the same revision is observed twice.
        """

        if revision != self._last_prepared_revision:
            raise _ContextLedgerError(
                f"observe called for un-prepared revision {revision}; "
                f"last prepared revision is {self._last_prepared_revision}"
            )
        if revision == self._observed_revision:
            raise _ContextLedgerError(f"observe called twice for revision {revision}")
        self._observed_revision = revision
        if usage is None:
            return
        self._anchor_revision = revision
        self._anchor_message_count = self._ledger.message_count
        self._anchor_input_tokens = usage.input_tokens

    async def prepare_request(self) -> PreparedContext:
        """Project the next normal Model Request and its Context Pressure."""

        request = ModelRequest(messages=self._ledger.history)
        projected, source = self._projected_input_tokens(request)
        pressure = ContextPressure(
            input_budget=self._policy.input_budget,
            projected_input_tokens=projected,
            usage_source=source,
        )
        self._last_prepared_revision = self._ledger.revision
        return PreparedContext(
            request=request,
            revision=self._ledger.revision,
            pressure=pressure,
        )

    def _projected_input_tokens(self, request: ModelRequest) -> tuple[int, UsageSource]:
        if self._anchor_input_tokens is None:
            return estimate_request_tokens(request), "estimated"
        delta = request.messages[self._anchor_message_count :]
        if not delta:
            return self._anchor_input_tokens, "reported"
        added = sum(estimate_message_tokens(message) for message in delta)
        return self._anchor_input_tokens + added, "estimated"


def estimate_request_tokens(request: ModelRequest) -> int:
    """Return a conservative deterministic input token estimate for one request."""

    message_tokens = sum(
        estimate_message_tokens(message) for message in request.messages
    )
    tool_tokens = sum(
        8 + _estimate_text_tokens(_dump(tool.to_json())) for tool in request.tools
    )
    return message_tokens + tool_tokens


def estimate_message_tokens(message: ModelMessage) -> int:
    """Return a conservative deterministic input token estimate for one message."""

    if isinstance(message, SystemMessage | UserMessage):
        return 4 + _estimate_text_tokens(_join_text(message.content))
    if isinstance(message, AssistantMessage):
        text_blocks = tuple(
            block for block in message.content if isinstance(block, TextBlock)
        )
        calls = tuple(block for block in message.content if isinstance(block, ToolCall))
        call_tokens = sum(
            8 + _estimate_text_tokens(_dump(call.arguments)) for call in calls
        )
        return 4 + _estimate_text_tokens(_join_text(text_blocks)) + call_tokens
    return 4 + sum(
        8
        + _estimate_text_tokens(
            _dump(result.error if result.error is not None else result.output)
        )
        for result in message.content
    )


def _estimate_text_tokens(text: str) -> int:
    cjk = sum(
        1
        for char in text
        if "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"
    )
    return cjk + math.ceil((len(text) - cjk) / 4)


def _join_text(blocks: tuple[TextBlock, ...]) -> str:
    return "".join(block.text for block in blocks)


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True)
