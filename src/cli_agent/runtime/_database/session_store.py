"""Fail-closed Session repository over the application state database.

``SessionStore`` owns the canonical journal: sessions are created with
revision 0, every journal append carries the caller's expected revision
and compares-and-swaps the session frontier inside one short
transaction, and loading re-validates session metadata plus the full
raw journal. Database failures, corrupted rows, unknown ids, and
revision conflicts raise classified Host-facing session errors instead
of degrading to a diagnostic-only trace.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from cli_agent.errors.session import (
    SessionConflictError,
    SessionCorruptedError,
    SessionNotFoundError,
    SessionPersistenceError,
)
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._session import (
    Session,
    SessionConfig,
    decode_journal_message,
    encode_journal_message,
)
from cli_agent.runtime.model import ModelMessage


class SessionStore:
    """Durable session and canonical-journal repository with optimistic writes.

    The store holds no runtime state: it only reads and writes the
    session tables. All mutations run inside one short transaction and
    commit the journal row together with the session revision frontier,
    so a failed append can never leave the two out of sync.
    """

    def __init__(self, database: _StateDatabase) -> None:
        """Hold the state database adapter shared with the Runtime."""

        self._database = database

    def create(
        self,
        session_id: str,
        workspace_id: str,
        config: SessionConfig,
    ) -> Session:
        """Create one durable session with revision 0.

        Args:
            session_id (`str`): The Host-visible session identifier.
            workspace_id (`str`): The session's stable workspace identity.
            config (`SessionConfig`): Stable configuration captured at
                session creation.

        Returns:
            The created `Session` with revision 0.

        Raises:
            SessionConflictError: If the session id already exists.
            SessionPersistenceError: If the database cannot write.
        """

        now = datetime.now(timezone.utc)
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    "INSERT INTO sessions "
                    "(session_id, workspace_id, revision, config, created_at, "
                    "updated_at) VALUES (?, ?, 0, ?, ?, ?)",
                    (
                        session_id,
                        workspace_id,
                        config.to_json(),
                        _isoformat(now),
                        _isoformat(now),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise SessionConflictError(session_id=session_id) from exc
        except sqlite3.Error as exc:
            raise _persistence_error("create", session_id, exc) from exc
        return Session(
            session_id=session_id,
            workspace_id=workspace_id,
            revision=0,
            config=config,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )

    def load(self, session_id: str) -> tuple[Session, tuple[ModelMessage, ...]]:
        """Load one session's metadata and decoded canonical journal.

        Every metadata field and journal row is validated: unknown
        schema versions, revision gaps, illegal roles, and broken
        payloads fail closed instead of returning partial data.

        Args:
            session_id (`str`): The session to load.

        Returns:
            The `Session` metadata and its gap-free journal decoded
            back into the original `ModelMessage` sequence.

        Raises:
            SessionNotFoundError: If the session id has no row.
            SessionCorruptedError: If metadata or journal validation
                fails.
            SessionPersistenceError: If the database cannot read.
        """

        try:
            with self._database.transaction() as connection:
                row = connection.execute(
                    "SELECT workspace_id, revision, config, created_at, "
                    "updated_at, archived_at FROM sessions "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFoundError(session_id=session_id)
                session = _decode_metadata(session_id, row)
                journal = connection.execute(
                    "SELECT revision, role, payload FROM session_journal "
                    "WHERE session_id = ? ORDER BY revision",
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _persistence_error("load", session_id, exc) from exc
        return session, _decode_journal(session_id, journal, session.revision)

    def list(self) -> tuple[Session, ...]:
        """List every durable session in creation order.

        Returns:
            The stored sessions ordered by ``created_at``.

        Raises:
            SessionCorruptedError: If one session's metadata fails
                validation.
            SessionPersistenceError: If the database cannot read.
        """

        try:
            with self._database.transaction() as connection:
                rows = connection.execute(
                    "SELECT session_id, workspace_id, revision, config, "
                    "created_at, updated_at, archived_at FROM sessions "
                    "ORDER BY created_at"
                ).fetchall()
        except sqlite3.Error as exc:
            raise _persistence_error("list", None, exc) from exc
        return tuple(
            _decode_metadata(session_id, tuple(row)) for session_id, *row in rows
        )

    def append(
        self,
        session_id: str,
        message: ModelMessage,
        *,
        expected_revision: int,
    ) -> int:
        """Append one message as the next journal entry.

        The session revision is advanced with a compare-and-swap guard:
        the journal row and the new frontier commit together, and the
        write only succeeds when the session revision still equals
        ``expected_revision``. Callers must continue from the returned
        revision; never increment a cached value and assume the commit
        succeeded.

        Args:
            session_id (`str`): The session to append to.
            message (`ModelMessage`): The message just appended to
                Context History; a ``SystemMessage`` is not a valid
                journal entry.
            expected_revision (`int`): The revision the caller believes
                is the session frontier.

        Returns:
            The new session revision after the append.

        Raises:
            SessionNotFoundError: If the session id has no row.
            SessionConflictError: If the session revision no longer
                equals ``expected_revision``.
            SessionCorruptedError: If the next journal revision is
                already occupied; the transaction rolls back.
            SessionPersistenceError: If the database cannot write.
        """

        role, payload = encode_journal_message(message)
        now = datetime.now(timezone.utc)
        next_revision = expected_revision + 1
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "UPDATE sessions SET revision = ?, updated_at = ? "
                    "WHERE session_id = ? AND revision = ?",
                    (
                        next_revision,
                        _isoformat(now),
                        session_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount == 0:
                    raise _classify_stale(
                        connection,
                        session_id,
                        expected_revision,
                    )
                try:
                    connection.execute(
                        "INSERT INTO session_journal "
                        "(session_id, revision, role, payload, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            session_id,
                            next_revision,
                            role,
                            payload,
                            _isoformat(now),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise SessionCorruptedError(
                        session_id=session_id,
                        reason=(f"journal revision {next_revision} already exists"),
                    ) from exc
        except sqlite3.Error as exc:
            raise _persistence_error("append", session_id, exc) from exc
        return next_revision


def _classify_stale(
    connection: sqlite3.Connection,
    session_id: str,
    expected_revision: int,
) -> SessionNotFoundError | SessionConflictError:
    row = connection.execute(
        "SELECT revision FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return SessionNotFoundError(session_id=session_id)
    return SessionConflictError(
        session_id=session_id,
        expected_revision=expected_revision,
        actual_revision=row[0],
    )


def _decode_metadata(session_id: str, row: tuple[object, ...]) -> Session:
    workspace_id, revision, config_json, created_at, updated_at, archived_at = row
    try:
        config = SessionConfig.from_json(config_json)
        created = _parse_timestamp(created_at)
        updated = _parse_timestamp(updated_at)
        archived = _parse_timestamp(archived_at) if archived_at is not None else None
    except ValueError as exc:
        raise SessionCorruptedError(
            session_id=session_id,
            reason=str(exc),
        ) from exc
    return Session(
        session_id=session_id,
        workspace_id=workspace_id,
        revision=revision,
        config=config,
        created_at=created,
        updated_at=updated,
        archived_at=archived,
    )


def _decode_journal(
    session_id: str,
    rows: list[tuple[object, ...]],
    frontier: int,
) -> tuple[ModelMessage, ...]:
    messages: list[ModelMessage] = []
    expected_revision = 1
    for revision, role, payload in rows:
        if revision != expected_revision:
            raise SessionCorruptedError(
                session_id=session_id,
                reason=(
                    "journal revision gap: expected "
                    f"{expected_revision}, found {revision}"
                ),
            )
        try:
            messages.append(decode_journal_message(role, payload))
        except ValueError as exc:
            raise SessionCorruptedError(
                session_id=session_id,
                reason=str(exc),
            ) from exc
        expected_revision += 1
    if expected_revision - 1 != frontier:
        raise SessionCorruptedError(
            session_id=session_id,
            reason=(
                "journal frontier mismatch: session revision is "
                f"{frontier}, journal ends at {expected_revision - 1}"
            ),
        )
    return tuple(messages)


def _persistence_error(
    operation: str,
    session_id: str | None,
    exc: sqlite3.Error,
) -> SessionPersistenceError:
    return SessionPersistenceError(
        operation=operation,
        session_id=session_id,
        exception_type=type(exc).__name__,
    )


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"timestamp is not a string: {value!r}")
    return datetime.fromisoformat(value)


def _isoformat(value: datetime) -> str:
    return value.isoformat()
