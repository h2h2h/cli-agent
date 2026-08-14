"""Host-facing classified errors."""

from __future__ import annotations

from collections.abc import Mapping

from cli_agent.errors.base import CliAgentError


class HostFacingError(CliAgentError):
    """A failure the model cannot fix that must escape the Runtime.

    Host-facing errors cross the ``AgentRuntime`` public boundary to the
    Host (CLI, TUI, or Server), which owns presentation of ``code``,
    ``message``, and the optional ``hint``. The Runtime never converts
    them into ``ToolResult`` errors for the model.

    Args:
        code (`str`): Stable machine-readable error semantic, for example
            ``backend_unavailable`` or ``session_persistence_failed``.
        message (`str`): Sanitized Host-readable summary.
        hint (`str | None`): Optional actionable hint the Host may show
            next to the message.
        details (`Mapping[str, object] | None`): Optional serializable
            structured context for the Host.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code, message, details=details)
        self.hint = hint

    def to_payload(self) -> dict[str, object]:
        """Return the stable payload exposed to the Host."""

        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "details": dict(self.details),
        }


class InternalRuntimeError(HostFacingError):
    """A sanitized report for an unexpected exception at a boundary.

    Raised only by boundary classification when an unexpected exception
    would otherwise cross a public Kernel or Runtime boundary. The full
    failure detail is recorded as a Diagnostic first; the error payload
    itself carries only the failing operation and the exception type.
    """
