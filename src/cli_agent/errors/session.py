"""Host-facing classified errors for durable session persistence.

Session persistence is fail-closed: the SessionStore never degrades a
failed write to a diagnostic-only trace. Database unavailability,
corrupted rows, unknown session ids, and optimistic-concurrency
conflicts cross the Runtime boundary as classified `HostFacingError`
instances with stable codes the Host can act on.
"""

from __future__ import annotations

from cli_agent.errors.host import HostFacingError


class SessionNotFoundError(HostFacingError):
    """Raised when a durable session id has no metadata row.

    Args:
        session_id (`str`): The session id that was not found.
    """

    def __init__(self, *, session_id: str) -> None:
        super().__init__(
            code="session_not_found",
            message="Session was not found.",
            details={"session_id": session_id},
        )


class SessionConflictError(HostFacingError):
    """Raised when a durable write's precondition no longer matches.

    Covers stale-revision writes, duplicate session creation, and
    duplicate model-call ids whose stored usage disagrees with the
    incoming payload. The expected and actual revisions are included
    when known; a duplicate create reports the conflict without a
    revision pair.

    Args:
        session_id (`str`): The session whose durable state changed.
        expected_revision (`int | None`): The revision the caller
            believed was current.
        actual_revision (`int | None`): The revision currently stored.
        model_call_id (`str | None`): The duplicate model-call id for
            idempotency conflicts.
        message (`str`): Human-readable summary of the conflict.
    """

    def __init__(
        self,
        *,
        session_id: str,
        expected_revision: int | None = None,
        actual_revision: int | None = None,
        model_call_id: str | None = None,
        message: str = "Session state changed since it was last read.",
    ) -> None:
        details: dict[str, object] = {"session_id": session_id}
        if expected_revision is not None:
            details["expected_revision"] = expected_revision
        if actual_revision is not None:
            details["actual_revision"] = actual_revision
        if model_call_id is not None:
            details["model_call_id"] = model_call_id
        super().__init__(
            code="session_conflict",
            message=message,
            hint="Reload the session before retrying the write.",
            details=details,
        )


class SessionCorruptedError(HostFacingError):
    """Raised when durable session data fails validation.

    Unknown schema versions, revision gaps, illegal roles, and broken
    payloads all fail closed through this error; the canonical journal
    is never silently rewritten or skipped.

    Args:
        session_id (`str`): The session holding the invalid data.
        reason (`str`): Serialized validation failure description.
    """

    def __init__(self, *, session_id: str, reason: str) -> None:
        super().__init__(
            code="session_corrupted",
            message="Stored session data could not be validated.",
            details={"session_id": session_id, "reason": reason},
        )


class SessionPersistenceError(HostFacingError):
    """Raised when the session database cannot read or write.

    Args:
        operation (`str`): The failing repository operation, for
            example ``append`` or ``load``.
        session_id (`str | None`): The affected session id when the
            operation targets one session.
        exception_type (`str | None`): The underlying database
            exception type name for Host-side correlation.
    """

    def __init__(
        self,
        *,
        operation: str,
        session_id: str | None = None,
        exception_type: str | None = None,
    ) -> None:
        details: dict[str, object] = {"operation": operation}
        if session_id is not None:
            details["session_id"] = session_id
        if exception_type is not None:
            details["exception_type"] = exception_type
        super().__init__(
            code="session_persistence_failed",
            message="Session persistence failed.",
            hint="Resolve the persistence failure before retrying.",
            details=details,
        )
