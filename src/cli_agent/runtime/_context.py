"""Host-visible Context budget and four-tier compaction policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Explicit input budget and compaction thresholds for one Session.

    Context Window must come from explicit Host configuration; Runtime never
    guesses it from a model name or endpoint.
    """

    context_window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    protected_tokens: int = 8_000
    snip_threshold: float = 0.60
    prune_threshold: float = 0.80
    summarize_threshold: float = 0.95
    snip_target: float = 0.55
    prune_target: float = 0.70
    summarize_target: float = 0.55
    minimum_reclaim_tokens: int = 4_096
    excluded_tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.output_reserve_tokens <= 0:
            raise ValueError("output_reserve_tokens must be a positive integer")
        if self.safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens must be a non-negative integer")
        if (
            self.context_window_tokens
            <= self.output_reserve_tokens + self.safety_margin_tokens
        ):
            raise ValueError(
                "context_window_tokens must exceed output reserve plus safety "
                "margin so the input budget is positive"
            )
        if not (
            0
            < self.snip_threshold
            < self.prune_threshold
            < self.summarize_threshold
            < 1
        ):
            raise ValueError(
                "thresholds must satisfy "
                "0 < snip_threshold < prune_threshold < summarize_threshold < 1"
            )
        if not 0 < self.snip_target < self.snip_threshold:
            raise ValueError("snip_target must be between 0 and snip_threshold")
        if not 0 < self.prune_target < self.prune_threshold:
            raise ValueError("prune_target must be between 0 and prune_threshold")
        if not 0 < self.summarize_target < self.summarize_threshold:
            raise ValueError(
                "summarize_target must be between 0 and summarize_threshold"
            )
        if self.protected_tokens < 0:
            raise ValueError("protected_tokens must be a non-negative integer")
        if self.minimum_reclaim_tokens < 0:
            raise ValueError("minimum_reclaim_tokens must be a non-negative integer")

    @property
    def input_budget(self) -> int:
        """Return input tokens available for one normal Model Request."""

        return (
            self.context_window_tokens
            - self.output_reserve_tokens
            - self.safety_margin_tokens
        )
