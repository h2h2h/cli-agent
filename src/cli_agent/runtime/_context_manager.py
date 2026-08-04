"""Session-scoped Context ownership: ledger, budget, and request preparation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import AbstractSet, Literal

from cli_agent.runtime._context import ContextPolicy
from cli_agent.runtime._tool_result_reducer import _ToolResultReducer
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    ModelRequest,
    ModelUsage,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

UsageSource = Literal["reported", "estimated"]
OperationReason = Literal["watermark", "oversized_result"]


class _ContextLedgerError(RuntimeError):
    """Raised when a Context History mutation breaks the model protocol."""


class ContextOverflowError(RuntimeError):
    """Raised when the projected request exceeds the Input Budget safely."""


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
class ContextOperation:
    """Structured statistics for one applied compaction pass."""

    tier: int
    revision_before: int
    revision_after: int
    input_tokens_before: int
    input_tokens_after: int
    entries_changed: int
    reason: OperationReason


@dataclass(frozen=True, slots=True)
class PreparedContext:
    """Immutable projection of one prepared normal Model Request."""

    request: ModelRequest
    revision: int
    pressure: ContextPressure
    operations: tuple[ContextOperation, ...] = ()


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
        self._reducer = _ToolResultReducer(context_policy.excluded_tools)
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
        """Project the next normal Model Request, compacting as needed."""

        operations: list[ContextOperation] = []
        while True:
            projected, _ = self._projected()
            budget = self._policy.input_budget
            changed = False
            if projected >= budget * self._policy.snip_threshold:
                projected, tier_operations = self._run_tier1()
                operations.extend(tier_operations)
                changed = bool(tier_operations)
            if projected >= budget * self._policy.prune_threshold:
                projected, tier_operations = self._run_tier2()
                operations.extend(tier_operations)
                changed = changed or bool(tier_operations)
            if not changed:
                break

        projected, source = self._projected()
        if projected > self._policy.input_budget:
            projected, oversized_operations = self._compact_oversized()
            operations.extend(oversized_operations)
            projected, source = self._projected()
            if projected > self._policy.input_budget:
                raise ContextOverflowError(
                    f"projected input of {projected} tokens exceeds the input "
                    f"budget of {self._policy.input_budget} tokens and cannot "
                    "be reduced safely"
                )

        request = ModelRequest(messages=self._ledger.history)
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
            operations=tuple(operations),
        )

    def _run_tier1(self) -> tuple[int, tuple[ContextOperation, ...]]:
        """Snip raw candidates until the Snip target, no candidates, or reclaim."""

        return self._run_reductions(state="raw", prune=False, tier=1)

    def _run_tier2(self) -> tuple[int, tuple[ContextOperation, ...]]:
        """Prune snipped candidates until the Prune target, no candidates, or reclaim."""

        return self._run_reductions(state="snipped", prune=True, tier=2)

    def _run_reductions(
        self,
        *,
        state: str,
        prune: bool,
        tier: int,
    ) -> tuple[int, tuple[ContextOperation, ...]]:
        policy = self._policy
        target = policy.input_budget * (
            policy.prune_target if prune else policy.snip_target
        )
        skipped: set[tuple[int, int]] = set()
        revision_before = self._ledger.revision
        projected_before, _ = self._projected()
        entries_changed = 0
        while True:
            projected, _ = self._projected()
            if projected < target:
                break
            candidate = self._next_candidate(state, skipped)
            if candidate is None:
                break
            message_index, result_index, call, result = candidate
            reclaim = self._candidate_reclaim(
                message_index,
                result_index,
                call,
                result,
                prune=prune,
            )
            if reclaim < policy.minimum_reclaim_tokens:
                skipped.add((message_index, result_index))
                continue
            self._apply_result(
                message_index,
                result_index,
                call,
                result,
                prune=prune,
            )
            entries_changed += 1
        if entries_changed == 0:
            return projected, ()
        projected_after, _ = self._projected()
        operation = ContextOperation(
            tier=tier,
            revision_before=revision_before,
            revision_after=self._ledger.revision,
            input_tokens_before=projected_before,
            input_tokens_after=projected_after,
            entries_changed=entries_changed,
            reason="watermark",
        )
        return projected_after, (operation,)

    def _compact_oversized(self) -> tuple[int, tuple[ContextOperation, ...]]:
        """Compact the newest compressible result to restore the Input Budget."""

        operations: list[ContextOperation] = []
        projected_after, _ = self._projected()
        while True:
            projected, _ = self._projected()
            if projected <= self._policy.input_budget:
                break
            candidate = self._newest_compressible()
            if candidate is None:
                break
            message_index, result_index, call, result = candidate
            prune = self._reducer.state_of(result) == "snipped"
            revision_before = self._ledger.revision
            self._apply_result(
                message_index,
                result_index,
                call,
                result,
                prune=prune,
            )
            projected_after, _ = self._projected()
            operations.append(
                ContextOperation(
                    tier=2 if prune else 1,
                    revision_before=revision_before,
                    revision_after=self._ledger.revision,
                    input_tokens_before=projected,
                    input_tokens_after=projected_after,
                    entries_changed=1,
                    reason="oversized_result",
                )
            )
        return projected_after, tuple(operations)

    def _next_candidate(
        self,
        state: str,
        skipped: AbstractSet[tuple[int, int]],
    ) -> tuple[int, int, ToolCall, ToolResult] | None:
        protected = self._protected_start()
        history = self._ledger.history
        for message_index in range(1, protected):
            message = history[message_index]
            if not isinstance(message, ToolResultMessage):
                continue
            previous = history[message_index - 1]
            if not isinstance(previous, AssistantMessage):
                continue
            calls = {
                block.call_id: block
                for block in previous.content
                if isinstance(block, ToolCall)
            }
            for result_index, result in enumerate(message.content):
                if (message_index, result_index) in skipped:
                    continue
                call = calls.get(result.call_id)
                if call is None or self._reducer.state_of(result) != state:
                    continue
                if not self._reducer.can_reduce(call, result):
                    continue
                return message_index, result_index, call, result
        return None

    def _newest_compressible(
        self,
    ) -> tuple[int, int, ToolCall, ToolResult] | None:
        history = self._ledger.history
        for message_index in range(len(history) - 1, 0, -1):
            message = history[message_index]
            if not isinstance(message, ToolResultMessage):
                continue
            previous = history[message_index - 1]
            if not isinstance(previous, AssistantMessage):
                continue
            calls = {
                block.call_id: block
                for block in previous.content
                if isinstance(block, ToolCall)
            }
            for result_index in range(len(message.content) - 1, -1, -1):
                result = message.content[result_index]
                call = calls.get(result.call_id)
                if call is None or not self._reducer.can_reduce(call, result):
                    continue
                return message_index, result_index, call, result
        return None

    def _candidate_reclaim(
        self,
        message_index: int,
        result_index: int,
        call: ToolCall,
        result: ToolResult,
        *,
        prune: bool,
    ) -> int:
        message = self._ledger.history[message_index]
        assert isinstance(message, ToolResultMessage)
        new_result = (
            self._reducer.prune(call, result)
            if prune
            else self._reducer.snip(call, result)
        )
        content = (
            message.content[:result_index]
            + (new_result,)
            + message.content[result_index + 1 :]
        )
        return estimate_message_tokens(message) - estimate_message_tokens(
            ToolResultMessage(content=content)
        )

    def _apply_result(
        self,
        message_index: int,
        result_index: int,
        call: ToolCall,
        result: ToolResult,
        *,
        prune: bool,
    ) -> None:
        message = self._ledger.history[message_index]
        assert isinstance(message, ToolResultMessage)
        new_result = (
            self._reducer.prune(call, result)
            if prune
            else self._reducer.snip(call, result)
        )
        content = (
            message.content[:result_index]
            + (new_result,)
            + message.content[result_index + 1 :]
        )
        self._ledger.replace_tool_results(message_index, content)

    def _protected_start(self) -> int:
        target = min(
            self._policy.protected_tokens,
            int(self._policy.input_budget * 0.20),
        )
        return self._ledger.protected_suffix_start(target)

    def _projected(self) -> tuple[int, UsageSource]:
        request = ModelRequest(messages=self._ledger.history)
        return self._projected_input_tokens(request)

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
