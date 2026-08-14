"""Fail-soft interim session journal writer over the application state database.

Each journal entry is persisted as its own row in ``session_journal``
inside a short transaction, preserving the original message payload
before any compaction. This adapter is the legacy write-only trace path
kept until the fail-closed ``SessionStore`` replaces it: every database
exception is swallowed and reported through ``on_diagnostic``, so a
failing write never interrupts the agent loop.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeAlias

from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._session import (
    SessionConfig,
    encode_journal_message,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import ModelMessage

_OnDiagnostic: TypeAlias = Callable[[RuntimeDiagnostic], None] | None


class _SessionHistory:
    """Persist one session's message trace in append-only journal rows.

    ``begin_session`` records session metadata once; ``append`` writes
    each message at the next journal revision inside one short
    transaction. Closing a session persists nothing: detach is a Runtime
    binding operation, not a session lifecycle state. Database errors are
    reported through ``session_history.write_failed``; callers decide whether
    an operation can safely remain fail-soft.
    """

    def __init__(
        self,
        database: _StateDatabase,
        on_diagnostic: _OnDiagnostic = None,
    ) -> None:
        """Hold the state database adapter shared with the Runtime."""

        self._database = database
        self._on_diagnostic = on_diagnostic

    def begin_session(
        self,
        session_id: str,
        workspace_id: str,
        system_prompt: str,
    ) -> bool | None:
        """Record session metadata once, keeping the first row on reuse.

        Args:
            session_id (`str`): The Host-provided session identifier.
            workspace_id (`str`): The session's stable workspace identity.
            system_prompt (`str`): The serialized system message blocks,
                stored for audit only.

        Returns:
            ``True`` when a new row was inserted, ``False`` when the Session
            already exists, or ``None`` when the fail-soft write failed.
        """

        try:
            now = _utc_now()
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO sessions "
                    "(session_id, workspace_id, revision, config, created_at, "
                    "updated_at) VALUES (?, ?, 0, ?, ?, ?)",
                    (
                        session_id,
                        workspace_id,
                        SessionConfig(system_prompt=system_prompt).to_json(),
                        now,
                        now,
                    ),
                )
            return cursor.rowcount == 1
        except Exception as exc:
            self._report("begin_session", session_id, exc)
            return None

    def append(self, session_id: str, message: ModelMessage) -> None:
        """Persist one message at the next journal revision.

        Args:
            session_id (`str`): The session the message belongs to.
            message (`ModelMessage`): The message just appended to Context
                History; a ``SystemMessage`` is not a valid journal entry.
        """

        try:
            role, payload = encode_journal_message(message)
            now = _utc_now()
            with self._database.transaction() as connection:
                (revision,) = connection.execute(
                    "SELECT revision FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute(
                    "INSERT INTO session_journal "
                    "(session_id, revision, role, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, revision + 1, role, payload, now),
                )
                connection.execute(
                    "UPDATE sessions SET revision = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (revision + 1, now, session_id),
                )
        except Exception as exc:
            self._report("append", session_id, exc)

    def _report(self, operation: str, session_id: str, exc: Exception) -> None:
        if self._on_diagnostic is None:
            return
        self._on_diagnostic(
            RuntimeDiagnostic(
                kind="session_history.write_failed",
                message=f"session history {operation} failed",
                detail={
                    "session_id": session_id,
                    "operation": operation,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
