"""Conversation ledger and model-protocol validation."""

from __future__ import annotations

from cli_agent.runtime._context.summarizer import summary_message
from cli_agent.runtime._context.tokens import estimate_message_tokens
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    SystemMessage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


class _ContextLedgerError(RuntimeError):
    """Raised when a Context History mutation breaks the model protocol."""


class _ContextLedger:
    """Own the ordered conversation and validate Turn and Tool Exchange edges."""

    def __init__(self, system_message: SystemMessage) -> None:
        self._messages: list[ModelMessage] = [system_message]
        self._revision = 0
        self._summary: str | None = None

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

    @property
    def summary(self) -> str | None:
        """Return the current summary text, or ``None`` before the first commit."""

        return self._summary

    @property
    def summary_frontier(self) -> int:
        """Return the message index after the last summarized content."""

        return 2 if self._summary is not None else 1

    def summarize_delta(
        self,
        protected_start: int,
        demand_tokens: int,
    ) -> tuple[int, int] | None:
        """Return the oldest closed-turn range meeting the token demand.

        Only complete closed Turns after the Summary Frontier and before the
        Protected Suffix are eligible; Active Turns and Tool Exchanges are
        never split.
        """

        frontier = self.summary_frontier
        accumulated = 0
        selected_start: int | None = None
        selected_end = frontier
        for start, end in self._turn_ranges():
            if start < frontier or end > protected_start:
                continue
            if not self._turn_is_closed(start, end):
                continue
            if selected_start is None:
                selected_start = start
            accumulated += sum(
                estimate_message_tokens(message)
                for message in self._messages[start:end]
            )
            selected_end = end
            if accumulated >= demand_tokens:
                break
        if selected_start is None:
            return None
        return selected_start, selected_end

    def commit_summary(self, summary_text: str, protected_start: int) -> None:
        """Atomically replace the summary and delete summarized delta turns."""

        self._messages = [
            self._messages[0],
            summary_message(summary_text),
            *self._messages[protected_start:],
        ]
        self._summary = summary_text
        self._revision += 1

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

    def replace_tool_results(
        self,
        message_index: int,
        content: tuple[ToolResult, ...],
    ) -> None:
        """Replace one Tool Result Message payload without changing call_ids."""

        self._messages[message_index] = ToolResultMessage(content=content)
        self._revision += 1

    def protected_suffix_start(self, target_tokens: int) -> int:
        """Return the message index where the Protected Suffix begins.

        The Protected Suffix accumulates complete User Turns from the end until
        ``target_tokens`` is met; Active Turns are always included.
        """

        accumulated = 0
        protected = len(self._messages)
        for start, end in reversed(self._turn_ranges()):
            accumulated += sum(
                estimate_message_tokens(message)
                for message in self._messages[start:end]
            )
            protected = start
            if self._turn_is_closed(start, end) and accumulated >= target_tokens:
                break
        return protected

    def _turn_ranges(self) -> tuple[tuple[int, int], ...]:
        starts = [
            index
            for index, message in enumerate(self._messages)
            if isinstance(message, UserMessage)
        ]
        ranges = []
        for position, start in enumerate(starts):
            end = (
                starts[position + 1]
                if position + 1 < len(starts)
                else len(self._messages)
            )
            ranges.append((start, end))
        return tuple(ranges)

    def _turn_is_closed(self, start: int, end: int) -> bool:
        del start
        last = self._messages[end - 1]
        if not isinstance(last, AssistantMessage):
            return False
        return not any(isinstance(block, ToolCall) for block in last.content)

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
