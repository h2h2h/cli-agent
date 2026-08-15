"""Host-facing classified errors for the active-session Runtime state machine.

Runtime lifecycle operations (new, resume, detach, archive, delete,
close) follow a formal state transition table; a call that would leave
the Runtime in an undefined state raises this error instead of
silently corrupting the active binding.
"""

from __future__ import annotations

from cli_agent.errors.host import HostFacingError


class RuntimeStateError(HostFacingError):
    """Raised when a lifecycle operation violates the state machine.

    Args:
        action (`str`): The operation that was attempted, for example
            ``run_turn`` or ``new_session``.
        state (`str`): The Runtime state at the time of the attempt.
        message (`str`): Human-readable summary of the violation.
    """

    def __init__(
        self,
        *,
        action: str,
        state: str,
        message: str,
    ) -> None:
        super().__init__(
            code="runtime_state",
            message=message,
            hint="Follow the Runtime lifecycle: new_session or resume_session before run_turn.",
            details={"action": action, "state": state},
        )
