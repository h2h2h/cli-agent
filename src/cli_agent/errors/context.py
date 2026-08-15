"""Host-facing classified errors for context budget exhaustion."""

from __future__ import annotations

from cli_agent.errors.host import HostFacingError


class ContextExhaustedError(HostFacingError):
    """Raised when a conversation cannot be reduced below the input budget.

    The ContextEngine first attempts deterministic reductions, semantic
    summarization, and overflow recovery; only when every safe path is
    exhausted does this error cross the Runtime boundary.

    Args:
        session_id (`str`): The session whose context cannot be reduced.
        projected_input_tokens (`int`): The smallest projection the
            engine could produce.
        input_budget (`int`): The configured per-request input budget.
    """

    def __init__(
        self,
        *,
        session_id: str,
        projected_input_tokens: int,
        input_budget: int,
    ) -> None:
        super().__init__(
            code="context_exhausted",
            message=(
                "The conversation cannot be reduced below the configured "
                "context budget."
            ),
            hint="Start a new session or shorten the conversation.",
            details={
                "session_id": session_id,
                "projected_input_tokens": projected_input_tokens,
                "input_budget": input_budget,
            },
        )
