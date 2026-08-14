"""Model-facing classified errors."""

from __future__ import annotations

from collections.abc import Mapping

from cli_agent.errors.base import CliAgentError


class ModelFacingError(CliAgentError):
    """An expected failure the model can observe and recover from itself.

    The EnvironmentKernel boundary translates these errors into
    ``ToolResult.error`` payloads, so a ``ModelFacingError`` never escapes
    the Runtime: the model reads ``code``, ``message``, and ``retryable``
    and adjusts its next action instead of the turn failing.

    Args:
        code (`str`): Stable machine-readable error semantic, for example
            ``invalid_argument`` or ``unknown_execution``.
        message (`str`): Sanitized model-readable summary.
        retryable (`bool`): Whether retrying the same call can succeed,
            for example after a pending queue drains.
        details (`Mapping[str, object] | None`): Optional serializable
            structured context for the model.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code, message, details=details)
        self.retryable = retryable

    def to_payload(self) -> dict[str, object]:
        """Return the stable payload exposed to the model."""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }
