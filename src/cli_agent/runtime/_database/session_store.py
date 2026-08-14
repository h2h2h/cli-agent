"""Fail-closed Session repository over the application state database.

``SessionStore`` owns the canonical journal plus two derived durable
domains: usage records are the accounting truth deduplicated by
``model_call_id``, and context snapshots are rebuildable caches
anchored to the journal revision they were derived from. Sessions are
created with revision 0, every journal append carries the caller's
expected revision and compares-and-swaps the session frontier inside
one short transaction, and loading re-validates session metadata plus
the full raw journal. Archive, unarchive, and delete keep separate
lifecycle semantics: archive only sets the metadata marker, and delete
removes the whole session through the schema's cascade. Database
failures, corrupted rows, unknown ids, and revision conflicts raise
classified Host-facing session errors instead of degrading to a
diagnostic-only trace.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from cli_agent.errors.session import (
    SessionArchivedError,
    SessionConflictError,
    SessionCorruptedError,
    SessionNotFoundError,
    SessionPersistenceError,
)
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._session import (
    ContextSnapshot,
    ModelCallUsage,
    Session,
    SessionConfig,
    decode_journal_message,
    encode_journal_message,
)
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
)

_INTERRUPTED_MESSAGE = (
    "The previous tool execution was interrupted. Its side effects are "
    "unknown. Inspect the workspace before retrying."
)

_INSERT_USAGE_SQL = (
    "INSERT INTO session_usage_records "
    "(model_call_id, session_id, purpose, input_tokens, output_tokens, "
    "created_at) VALUES (?, ?, ?, ?, ?, ?)"
)


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

    def list(self, *, include_archived: bool = False) -> tuple[Session, ...]:
        """List durable sessions in creation order.

        Archived sessions are excluded by default because resume only
        considers live sessions; management views pass
        ``include_archived=True``.

        Args:
            include_archived (`bool`): Whether to also return archived
                sessions.

        Returns:
            The stored sessions ordered by ``created_at``.

        Raises:
            SessionCorruptedError: If one session's metadata fails
                validation.
            SessionPersistenceError: If the database cannot read.
        """

        archived_filter = "" if include_archived else " WHERE archived_at IS NULL"
        try:
            with self._database.transaction() as connection:
                rows = connection.execute(
                    "SELECT session_id, workspace_id, revision, config, "
                    "created_at, updated_at, archived_at FROM sessions"
                    f"{archived_filter} ORDER BY created_at"
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
        usage: ModelCallUsage | None = None,
    ) -> int:
        """Append one message as the next journal entry.

        The session revision is advanced with a compare-and-swap guard:
        the journal row and the new frontier commit together, and the
        write only succeeds when the session revision still equals
        ``expected_revision``. Callers must continue from the returned
        revision; never increment a cached value and assume the commit
        succeeded.

        When ``usage`` is provided, the message and the usage record
        commit atomically: a crash can never leave one without the
        other. The commit is idempotent by ``model_call_id``: retrying
        an already-committed call returns the current revision without
        appending the message again or double-counting tokens, while a
        duplicate id whose stored usage disagrees raises
        `SessionConflictError`.

        Args:
            session_id (`str`): The session to append to.
            message (`ModelMessage`): The message just appended to
                Context History; a ``SystemMessage`` is not a valid
                journal entry.
            expected_revision (`int`): The revision the caller believes
                is the session frontier.
            usage (`ModelCallUsage | None`): The model-call accounting
                record that belongs to ``message`` and must commit in
                the same transaction; ``None`` for calls without usage.

        Returns:
            The new session revision after the append.

        Raises:
            SessionNotFoundError: If the session id has no row.
            SessionConflictError: If the session revision no longer
                equals ``expected_revision``, or a duplicate
                ``model_call_id`` carries different usage data.
            SessionCorruptedError: If the next journal revision is
                already occupied; the transaction rolls back.
            SessionPersistenceError: If the database cannot write.
        """

        role, payload = encode_journal_message(message)
        now = datetime.now(timezone.utc)
        next_revision = expected_revision + 1
        try:
            with self._database.transaction() as connection:
                if usage is not None:
                    existing = _existing_usage_row(connection, usage.model_call_id)
                    if existing is not None:
                        if existing != _usage_row_values(usage):
                            raise _usage_conflict(session_id, usage)
                        (frontier,) = connection.execute(
                            "SELECT revision FROM sessions WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        return frontier
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
                if usage is not None:
                    try:
                        connection.execute(
                            _INSERT_USAGE_SQL,
                            _usage_insert_values(usage, now),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise _usage_conflict(session_id, usage) from exc
        except sqlite3.Error as exc:
            raise _persistence_error("append", session_id, exc) from exc
        return next_revision

    def load_usage_records(self, session_id: str) -> tuple[ModelCallUsage, ...]:
        """Load every usage record for one session in creation order.

        Args:
            session_id (`str`): The session to read accounting for.

        Returns:
            The stored `ModelCallUsage` records, empty for unknown
            sessions.

        Raises:
            SessionCorruptedError: If one record fails validation.
            SessionPersistenceError: If the database cannot read.
        """

        try:
            with self._database.transaction() as connection:
                rows = connection.execute(
                    "SELECT model_call_id, session_id, purpose, input_tokens, "
                    "output_tokens, created_at FROM session_usage_records "
                    "WHERE session_id = ? ORDER BY created_at",
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _persistence_error("load_usage_records", session_id, exc) from exc
        records: list[ModelCallUsage] = []
        for (
            model_call_id,
            sid,
            purpose,
            input_tokens,
            output_tokens,
            created_at,
        ) in rows:
            try:
                records.append(
                    ModelCallUsage(
                        model_call_id=model_call_id,
                        session_id=sid,
                        purpose=purpose,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        created_at=_parse_timestamp(created_at),
                    )
                )
            except ValueError as exc:
                raise SessionCorruptedError(
                    session_id=session_id,
                    reason=str(exc),
                ) from exc
        return tuple(records)

    def usage_total(self, session_id: str) -> tuple[int, int]:
        """Project the aggregate token usage for one session.

        The aggregate is a rebuildable projection of the usage records;
        the records themselves are the accounting truth.

        Args:
            session_id (`str`): The session to aggregate.

        Returns:
            The ``(input_tokens, output_tokens)`` sums across all
            stored records, ``(0, 0)`` for unknown sessions.

        Raises:
            SessionPersistenceError: If the database cannot read.
        """

        try:
            with self._database.transaction() as connection:
                (input_tokens, output_tokens) = connection.execute(
                    "SELECT COALESCE(SUM(input_tokens), 0), "
                    "COALESCE(SUM(output_tokens), 0) "
                    "FROM session_usage_records WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _persistence_error("usage_total", session_id, exc) from exc
        return input_tokens, output_tokens

    def load_snapshot(
        self,
        session_id: str,
        *,
        derivation_version: str,
    ) -> ContextSnapshot | None:
        """Load one context snapshot, or ``None`` when it is unusable.

        A snapshot is usable only when its stored derivation version
        matches the caller's, its payload decodes cleanly, and its
        source revision does not exceed the session frontier. Any
        violation returns ``None`` without touching the canonical
        journal, so callers always fall back to rebuilding from the
        raw journal.

        Args:
            session_id (`str`): The session to read the snapshot of.
            derivation_version (`str`): The derivation logic version
                the caller currently implements.

        Returns:
            The decoded `ContextSnapshot`, or ``None`` when no usable
            snapshot exists.

        Raises:
            SessionPersistenceError: If the database cannot read.
        """

        try:
            with self._database.transaction() as connection:
                row = connection.execute(
                    "SELECT source_revision, derivation_version, payload "
                    "FROM session_context_snapshots WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    return None
                source_revision, stored_version, payload = row
                if stored_version != derivation_version:
                    return None
                session_row = connection.execute(
                    "SELECT revision FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session_row is None or source_revision > session_row[0]:
                    return None
                try:
                    return ContextSnapshot.from_json(
                        payload,
                        session_id=session_id,
                        source_revision=source_revision,
                        derivation_version=stored_version,
                    )
                except ValueError:
                    return None
        except sqlite3.Error as exc:
            raise _persistence_error("load_snapshot", session_id, exc) from exc

    def save_snapshot(
        self,
        snapshot: ContextSnapshot,
        *,
        expected_revision: int,
        usage: ModelCallUsage | None = None,
    ) -> None:
        """Replace one session's context snapshot.

        The snapshot only commits when the session revision still
        equals ``expected_revision``, so a stale derivation can never
        overwrite a newer one. Snapshots never advance the journal
        frontier. When ``usage`` is provided, the compaction call's
        accounting record commits in the same transaction and is
        idempotent by ``model_call_id``.

        Args:
            snapshot (`ContextSnapshot`): The derived conversation
                projection to store; its ``source_revision`` must not
                exceed ``expected_revision``.
            expected_revision (`int`): The revision the caller believes
                is the session frontier.
            usage (`ModelCallUsage | None`): The compaction call's
                accounting record committing beside the snapshot.

        Raises:
            ValueError: If ``snapshot.source_revision`` exceeds
                ``expected_revision``.
            SessionNotFoundError: If the session id has no row.
            SessionConflictError: If the session revision no longer
                equals ``expected_revision``, or a duplicate
                ``model_call_id`` carries different usage data.
            SessionPersistenceError: If the database cannot write.
        """

        if snapshot.source_revision > expected_revision:
            raise ValueError(
                f"snapshot source revision {snapshot.source_revision} "
                f"exceeds session revision {expected_revision}"
            )
        now = datetime.now(timezone.utc)
        try:
            with self._database.transaction() as connection:
                insert_usage = usage is not None
                if usage is not None:
                    existing = _existing_usage_row(connection, usage.model_call_id)
                    if existing is not None:
                        if existing != _usage_row_values(usage):
                            raise _usage_conflict(snapshot.session_id, usage)
                        insert_usage = False
                cursor = connection.execute(
                    "UPDATE sessions SET updated_at = ? "
                    "WHERE session_id = ? AND revision = ?",
                    (
                        _isoformat(now),
                        snapshot.session_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount == 0:
                    raise _classify_stale(
                        connection,
                        snapshot.session_id,
                        expected_revision,
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO session_context_snapshots "
                    "(session_id, source_revision, derivation_version, payload, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot.session_id,
                        snapshot.source_revision,
                        snapshot.derivation_version,
                        snapshot.to_json(),
                        _isoformat(now),
                    ),
                )
                if insert_usage:
                    try:
                        connection.execute(
                            _INSERT_USAGE_SQL,
                            _usage_insert_values(usage, now),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise _usage_conflict(snapshot.session_id, usage) from exc
        except sqlite3.Error as exc:
            raise _persistence_error(
                "save_snapshot",
                snapshot.session_id,
                exc,
            ) from exc

    def invalidate_snapshot(self, session_id: str) -> None:
        """Delete one session's context snapshot.

        Invalidation only removes the rebuildable derived cache; the
        canonical journal is never touched.

        Args:
            session_id (`str`): The session whose snapshot is stale.

        Raises:
            SessionPersistenceError: If the database cannot write.
        """

        try:
            with self._database.transaction() as connection:
                connection.execute(
                    "DELETE FROM session_context_snapshots WHERE session_id = ?",
                    (session_id,),
                )
        except sqlite3.Error as exc:
            raise _persistence_error(
                "invalidate_snapshot",
                session_id,
                exc,
            ) from exc

    def archive(self, session_id: str) -> None:
        """Mark one session archived.

        Archiving only sets the ``archived_at`` metadata marker: the
        journal, usage records, and snapshots stay untouched, and the
        session stays resumable after an explicit unarchive.

        Args:
            session_id (`str`): The session to archive.

        Raises:
            SessionNotFoundError: If the session id has no row.
            SessionPersistenceError: If the database cannot write.
        """

        now = datetime.now(timezone.utc)
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "UPDATE sessions SET archived_at = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (_isoformat(now), _isoformat(now), session_id),
                )
        except sqlite3.Error as exc:
            raise _persistence_error("archive", session_id, exc) from exc
        if cursor.rowcount == 0:
            raise SessionNotFoundError(session_id=session_id)

    def unarchive(self, session_id: str) -> None:
        """Clear one session's archived marker.

        Args:
            session_id (`str`): The session to unarchive.

        Raises:
            SessionNotFoundError: If the session id has no row.
            SessionPersistenceError: If the database cannot write.
        """

        now = datetime.now(timezone.utc)
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "UPDATE sessions SET archived_at = NULL, updated_at = ? "
                    "WHERE session_id = ?",
                    (_isoformat(now), session_id),
                )
        except sqlite3.Error as exc:
            raise _persistence_error("unarchive", session_id, exc) from exc
        if cursor.rowcount == 0:
            raise SessionNotFoundError(session_id=session_id)

    def delete(self, session_id: str) -> None:
        """Delete one session and all its durable data.

        The journal, usage records, and context snapshots are removed
        by the schema's foreign-key cascade inside the same transaction
        as the metadata delete. Detaching an active binding before the
        delete is the Runtime's responsibility.

        Args:
            session_id (`str`): The session to delete.

        Raises:
            SessionNotFoundError: If the session id has no row.
            SessionPersistenceError: If the database cannot write.
        """

        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
        except sqlite3.Error as exc:
            raise _persistence_error("delete", session_id, exc) from exc
        if cursor.rowcount == 0:
            raise SessionNotFoundError(session_id=session_id)

    def repair_interrupted_execution(
        self,
        session_id: str,
        *,
        expected_revision: int,
    ) -> int:
        """Repair one session's crash frontier for interrupted tool calls.

        Scans the journal for tool calls that never received a matching
        tool result - the frontier left behind when a process crashed
        between an assistant tool call and its results - and appends
        one synthetic ``execution_interrupted`` ToolResultMessage through
        the usual compare-and-swap guard, so a concurrent writer can
        never be silently overwritten. The repair never executes tools:
        the synthetic results only tell the model that the side effects
        are unknown. Resuming twice only appends once, because the
        synthetic results resolve the frontier.

        Args:
            session_id (`str`): The session to repair.
            expected_revision (`int`): The revision the caller believes
                is the session frontier.

        Returns:
            The new revision after a repair append, or the unchanged
            current revision when the frontier already holds no
            unresolved tool calls.

        Raises:
            SessionNotFoundError: If the session id has no row.
            SessionArchivedError: If the session is archived; resume
                requires an explicit unarchive first.
            SessionConflictError: If the session revision no longer
                equals ``expected_revision``.
            SessionCorruptedError: If the journal structure is invalid.
            SessionPersistenceError: If the database cannot read or
                write.
        """

        now = datetime.now(timezone.utc)
        try:
            with self._database.transaction() as connection:
                session_row = connection.execute(
                    "SELECT revision, archived_at FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session_row is None:
                    raise SessionNotFoundError(session_id=session_id)
                frontier, archived_at = session_row
                if archived_at is not None:
                    raise SessionArchivedError(session_id=session_id)
                rows = connection.execute(
                    "SELECT revision, role, payload FROM session_journal "
                    "WHERE session_id = ? ORDER BY revision",
                    (session_id,),
                ).fetchall()
                pending = _unresolved_call_ids(session_id, rows)
                if not pending:
                    return frontier
                cursor = connection.execute(
                    "UPDATE sessions SET revision = ?, updated_at = ? "
                    "WHERE session_id = ? AND revision = ?",
                    (
                        expected_revision + 1,
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
                _, payload = encode_journal_message(
                    ToolResultMessage(
                        content=tuple(
                            ToolResult(
                                call_id=call_id,
                                output=_interrupted_output(),
                            )
                            for call_id in pending
                        )
                    )
                )
                connection.execute(
                    "INSERT INTO session_journal "
                    "(session_id, revision, role, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        expected_revision + 1,
                        "tool_result",
                        payload,
                        _isoformat(now),
                    ),
                )
        except sqlite3.Error as exc:
            raise _persistence_error(
                "repair_interrupted_execution",
                session_id,
                exc,
            ) from exc
        return expected_revision + 1


def _unresolved_call_ids(
    session_id: str,
    rows: list[tuple[object, ...]],
) -> list[str]:
    """Return tool call ids that never received a matching tool result.

    The journal structure is validated while scanning: a tool call id
    may appear at most once, and every tool result must reference a
    preceding tool call. Structural anomalies fail closed.
    """

    seen: set[str] = set()
    pending: dict[str, None] = {}
    for revision, role, payload in rows:
        try:
            message = decode_journal_message(role, payload)
        except ValueError as exc:
            raise SessionCorruptedError(
                session_id=session_id,
                reason=str(exc),
            ) from exc
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if not isinstance(block, ToolCall):
                    continue
                if block.call_id in seen:
                    raise SessionCorruptedError(
                        session_id=session_id,
                        reason=f"duplicate tool call id: {block.call_id}",
                    )
                seen.add(block.call_id)
                pending[block.call_id] = None
        elif isinstance(message, ToolResultMessage):
            for result in message.content:
                if result.call_id not in pending:
                    raise SessionCorruptedError(
                        session_id=session_id,
                        reason=(
                            f"tool result without preceding tool call: {result.call_id}"
                        ),
                    )
                del pending[result.call_id]
    return list(pending)


def _interrupted_output() -> dict[str, str]:
    return {
        "code": "execution_interrupted",
        "message": _INTERRUPTED_MESSAGE,
        "outcome": "unknown",
    }


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


def _existing_usage_row(
    connection: sqlite3.Connection,
    model_call_id: str,
) -> tuple[object, ...] | None:
    return connection.execute(
        "SELECT session_id, purpose, input_tokens, output_tokens "
        "FROM session_usage_records WHERE model_call_id = ?",
        (model_call_id,),
    ).fetchone()


def _usage_row_values(usage: ModelCallUsage) -> tuple[object, ...]:
    return (
        usage.session_id,
        usage.purpose,
        usage.input_tokens,
        usage.output_tokens,
    )


def _usage_insert_values(
    usage: ModelCallUsage,
    now: datetime,
) -> tuple[object, ...]:
    return (
        usage.model_call_id,
        usage.session_id,
        usage.purpose,
        usage.input_tokens,
        usage.output_tokens,
        _isoformat(now),
    )


def _usage_conflict(
    session_id: str,
    usage: ModelCallUsage,
) -> SessionConflictError:
    return SessionConflictError(
        session_id=session_id,
        model_call_id=usage.model_call_id,
        message=(
            "A model call with this id was already committed with different data."
        ),
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
