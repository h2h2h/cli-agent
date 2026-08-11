"""Session-scoped Context ownership: ledger, budget, and request preparation."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import AbstractSet, Literal

from cli_agent.runtime._context import ContextPolicy
from cli_agent.runtime._context_summarizer import (
    _ContextSummarizer,
    build_summary_messages,
    has_all_summary_sections,
    summary_message,
)
from cli_agent.runtime._tool_result_reducer import _ToolResultReducer
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    ModelProvider,
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
OperationReason = Literal["watermark", "oversized_result", "overflow_recovery"]


class _ContextLedgerError(RuntimeError):
    """Raised when a Context History mutation breaks the model protocol."""


class ContextOverflowError(RuntimeError):
    """Raised when the projected request exceeds the Input Budget safely."""


@dataclass(frozen=True, slots=True)
class SessionUsage:
    """Session-cumulative input and output tokens across all model calls."""

    input_tokens: int
    output_tokens: int


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
    usage_source: UsageSource = "estimated"
    turns_summarized: int = 0
    summary_input_tokens: int | None = None
    summary_output_tokens: int | None = None


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


class _ContextManager:
    """Own Context History, revisions, usage anchors, and request preparation."""

    def __init__(
        self,
        *,
        system_message: SystemMessage,
        context_policy: ContextPolicy,
        provider: ModelProvider,
        session_id: str,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
        on_append: Callable[[ModelMessage], None] | None = None,
    ) -> None:
        self._ledger = _ContextLedger(system_message)
        self._policy = context_policy
        self._reducer = _ToolResultReducer(context_policy.excluded_tools)
        self._summarizer = _ContextSummarizer(provider)
        self._session_id = session_id
        self._on_diagnostic = on_diagnostic
        self._on_append = on_append
        self._anchor_revision: int | None = None
        self._anchor_message_count = 0
        self._anchor_input_tokens: int | None = None
        self._last_prepared_revision: int | None = None
        self._observed_revision: int | None = None
        self._last_summarize_revision: int | None = None
        self._usage = SessionUsage(input_tokens=0, output_tokens=0)

    @property
    def usage(self) -> SessionUsage:
        """Return the session-cumulative token usage snapshot."""

        return self._usage

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
        if self._on_append is not None:
            self._on_append(message)

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
        self._accumulate(usage)

    async def prepare_request(self) -> PreparedContext:
        """Project the next normal Model Request, compacting as needed."""

        operations: list[ContextOperation] = []

        # ====================================================================
        # Step 1: Gradually reclaim space with deterministic tool-result reductions
        # ====================================================================
        # Snip and prune only change Tool Result content without breaking Turn
        # boundaries or Tool Call/Tool Result pairs, so they take precedence
        # over summarization, which may discard semantic details. Re-project
        # the request after every pass: one reduction may cross another
        # watermark or expose another batch of compressible results.
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

        # ====================================================================
        # Step 2: Summarize the oldest complete Turns when reductions are not enough
        # ====================================================================
        # Tier 3 only processes closed Turns and preserves the recent Protected
        # Suffix, avoiding interruption of the active interaction or leaving
        # unmatched tool calls. Re-project after a successful summary because
        # the summary also consumes input tokens and invalidates the usage anchor.
        projected, source = self._projected()
        if projected >= budget * self._policy.summarize_threshold:
            summarized, summary_operations = await self._run_tier3(projected)
            operations.extend(summary_operations)
            if summarized:
                projected, source = self._projected()

        # ====================================================================
        # Step 3: Reduce the newest compressible results if the hard budget is exceeded
        # ====================================================================
        # This is the fallback for oversized Tool Results: reduce results from
        # newest to oldest to preserve recent context where possible. If all
        # safe reductions are exhausted and the request still exceeds the
        # budget, fail explicitly instead of producing an unusable request.
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

        # ====================================================================
        # Step 4: Finalize the request and expose the compaction results
        # ====================================================================
        # Build the request from the Ledger's latest immutable snapshot. The
        # revision and pressure record this projection so that a later observe
        # can anchor the Provider's reported usage to this exact request.
        request = ModelRequest(messages=self._ledger.history)
        pressure = ContextPressure(
            input_budget=self._policy.input_budget,
            projected_input_tokens=projected,
            usage_source=source,
        )
        self._last_prepared_revision = self._ledger.revision
        self._emit_operations(operations)
        return PreparedContext(
            request=request,
            revision=self._ledger.revision,
            pressure=pressure,
            operations=tuple(operations),
        )

    async def force_prepare(self) -> PreparedContext | None:
        """Recover a reported Context Overflow with aggressive compaction.

        Invalidates the reported usage anchor, exhausts all deterministic
        reductions, runs Tier 3 when a complete prefix exists, and re-checks
        the hard Input Budget. Returns ``None`` when the request cannot be
        reduced safely.
        """

        self._invalidate_anchor()
        while True:
            changed = False
            _, tier_operations = self._run_tier1(force=True)
            if tier_operations:
                changed = True
                self._emit_operations(tier_operations)
            _, tier_operations = self._run_tier2(force=True)
            if tier_operations:
                changed = True
                self._emit_operations(tier_operations)
            if not changed:
                break
        projected, _ = self._projected()
        _, summary_operations = await self._run_tier3(projected, force=True)
        self._emit_operations(summary_operations)
        projected, source = self._projected()
        if projected > self._policy.input_budget:
            projected, oversized_operations = self._compact_oversized()
            self._emit_operations(oversized_operations)
            if projected > self._policy.input_budget:
                return None
        self._last_prepared_revision = self._ledger.revision
        return PreparedContext(
            request=ModelRequest(messages=self._ledger.history),
            revision=self._ledger.revision,
            pressure=ContextPressure(
                input_budget=self._policy.input_budget,
                projected_input_tokens=projected,
                usage_source=source,
            ),
        )

    def _run_tier1(
        self,
        *,
        force: bool = False,
    ) -> tuple[int, tuple[ContextOperation, ...]]:
        """Snip raw candidates until the Snip target, no candidates, or reclaim."""

        # ====================================================================
        # Tier 1: Snip raw execution snapshots
        # ====================================================================
        # Select raw, eligible Tool Results and replace each execution
        # snapshot with bounded head/tail chunks plus execution metadata. This
        # preserves the result's identity and re-read hints while removing the
        # largest unstructured payloads without involving a model.
        return self._run_reductions(state="raw", prune=False, tier=1, force=force)

    def _run_tier2(
        self,
        *,
        force: bool = False,
    ) -> tuple[int, tuple[ContextOperation, ...]]:
        """Prune snipped candidates until the Prune target, no candidates, or reclaim."""

        # ====================================================================
        # Tier 2: Prune already-snipped execution snapshots
        # ====================================================================
        # Continue the monotonic reduction for results that have already been
        # snipped. Keep only the execution identity and status metadata, so the
        # model can still identify the result and re-read it when necessary.
        return self._run_reductions(state="snipped", prune=True, tier=2, force=force)

    def _run_reductions(
        self,
        *,
        state: str,
        prune: bool,
        tier: int,
        force: bool = False,
    ) -> tuple[int, tuple[ContextOperation, ...]]:
        # Tier 1 and Tier 2 share the same monotonic loop. Each tier has its
        # own lower target: normal preparation stops once the projection is
        # below that target, while force mode exhausts every safe candidate.
        policy = self._policy
        target = policy.input_budget * (
            policy.prune_target if prune else policy.snip_target
        )
        skipped: set[tuple[int, int]] = set()
        revision_before = self._ledger.revision
        projected_before, usage_source = self._projected()
        entries_changed = 0
        while True:
            projected, _ = self._projected()
            if not force and projected < target:
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
            # Avoid spending a revision on a small reduction during normal
            # watermark compaction. Overflow recovery deliberately ignores
            # this minimum and keeps reducing until no candidate remains.
            if not force and reclaim < policy.minimum_reclaim_tokens:
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
            reason="overflow_recovery" if force else "watermark",
            usage_source=usage_source,
        )
        return projected_after, (operation,)

    async def _run_tier3(
        self,
        projected_before: int,
        *,
        force: bool = False,
    ) -> tuple[bool, tuple[ContextOperation, ...]]:
        """Summarize the oldest closed Turns when deterministic tiers cannot."""

        # ====================================================================
        # Tier 3: Replace the oldest closed Turns with a bounded summary
        # ====================================================================
        # Semantic compression is intentionally delayed until deterministic
        # Tool Result reductions are insufficient. Summarize only the oldest
        # complete Turns after the current summary frontier and before the
        # protected recent suffix; active Turns and tool exchanges stay intact.
        policy = self._policy
        if not force and self._ledger.revision == self._last_summarize_revision:
            return False, ()
        protected_start = self._protected_start()
        demand = (
            2**63
            if force
            else max(
                1,
                int(projected_before - policy.summarize_target * policy.input_budget),
            )
        )
        delta_range = self._ledger.summarize_delta(protected_start, demand)
        if delta_range is None:
            return False, ()
        delta_start, delta_end = delta_range
        history = self._ledger.history
        protected_tokens = sum(
            estimate_message_tokens(message) for message in history[protected_start:]
        )
        max_summary_tokens = max(
            1, int(policy.summarize_target * policy.input_budget) - protected_tokens
        )
        # Feed the previous summary and the selected delta to the summarizer,
        # while capping the new summary so that it fits beside the protected
        # suffix within the summary target.
        prompt = build_summary_messages(
            old_summary=self._ledger.summary,
            delta=history[delta_start:delta_end],
            max_tokens=max_summary_tokens,
        )
        summary_result = await self._summarizer.summarize(prompt)
        self._last_summarize_revision = self._ledger.revision
        if summary_result is None:
            self._emit_compaction_failed()
            return False, ()
        summary = summary_result.message
        text = "".join(
            block.text for block in summary.content if isinstance(block, TextBlock)
        )
        if not text.strip() or not has_all_summary_sections(text):
            self._emit_compaction_failed()
            return False, ()
        if estimate_message_tokens(summary) > max_summary_tokens:
            self._emit_compaction_failed()
            return False, ()
        turns = sum(
            1
            for message in history[delta_start:delta_end]
            if isinstance(message, UserMessage)
        )
        revision_before = self._ledger.revision
        # Validate the complete summary before atomically replacing the old
        # summary and summarized Turns. Any failed validation leaves history
        # unchanged and allows the caller to continue with the hard-budget
        # fallback.
        self._ledger.commit_summary(text.strip(), protected_start)
        self._last_summarize_revision = self._ledger.revision
        self._invalidate_anchor()
        self._accumulate(summary_result.usage)
        projected_after, _ = self._projected()
        operation = ContextOperation(
            tier=3,
            revision_before=revision_before,
            revision_after=self._ledger.revision,
            input_tokens_before=projected_before,
            input_tokens_after=projected_after,
            entries_changed=turns,
            reason="overflow_recovery" if force else "watermark",
            turns_summarized=turns,
            summary_input_tokens=(
                summary_result.usage.input_tokens
                if summary_result.usage is not None
                else None
            ),
            summary_output_tokens=(
                summary_result.usage.output_tokens
                if summary_result.usage is not None
                else None
            ),
        )
        return True, (operation,)

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

    def _invalidate_anchor(self) -> None:
        self._anchor_revision = None
        self._anchor_message_count = 0
        self._anchor_input_tokens = None

    def _accumulate(self, usage: ModelUsage | None) -> None:
        """Add one reported completion usage to the session total."""

        if usage is None:
            return
        self._usage = SessionUsage(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
        )

    def _emit_operations(
        self,
        operations: Sequence[ContextOperation],
    ) -> None:
        if self._on_diagnostic is None:
            return
        for operation in operations:
            kind = {
                1: "context.snipped",
                2: "context.pruned",
                3: "context.summarized",
            }[operation.tier]
            if operation.reason == "oversized_result":
                kind = "context.oversized_result"
            self._on_diagnostic(
                RuntimeDiagnostic(
                    kind=kind,
                    message=(
                        f"context compaction released "
                        f"{operation.input_tokens_before - operation.input_tokens_after}"
                        " projected input tokens"
                    ),
                    detail={
                        "session_id": self._session_id,
                        "revision_before": operation.revision_before,
                        "revision_after": operation.revision_after,
                        "tier": operation.tier,
                        "usage_source": operation.usage_source,
                        "input_tokens_before": operation.input_tokens_before,
                        "input_tokens_after": operation.input_tokens_after,
                        "entries_changed": operation.entries_changed,
                        "turns_summarized": operation.turns_summarized,
                        "reason": operation.reason,
                    },
                )
            )

    def _emit_compaction_failed(self) -> None:
        if self._on_diagnostic is None:
            return
        self._on_diagnostic(
            RuntimeDiagnostic(
                kind="context.compaction_failed",
                message="tier 3 summarization failed; history is unchanged",
                detail={
                    "session_id": self._session_id,
                    "revision": self._ledger.revision,
                    "tier": 3,
                },
            )
        )

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
