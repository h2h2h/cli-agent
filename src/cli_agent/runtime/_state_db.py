"""Runtime-owned application state database adapter.

The database lives at ``~/.config/cli-agent/state.sqlite3`` and is not bound
to one capability: future application state, such as Session History, can add
tables through the same explicit migration boundary. Library summaries are
stored in the ``library_summary_cache`` table by the ``_SummaryCache``
adapter, never through generic SQL exposed here.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cli_agent.runtime._capability.workspace import (
    _ensure_real_directory,
    _ensure_real_file,
)

_BUSY_TIMEOUT_SECONDS = 5.0

_MIGRATIONS: tuple[str, ...] = (
    """CREATE TABLE library_summary_cache (
        fingerprint TEXT PRIMARY KEY,
        subject_kind TEXT NOT NULL
            CHECK (subject_kind IN ('file', 'directory')),
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT NOT NULL
    )""",
)


class _StateDatabase:
    """SQLite application state database with explicit versioned migrations.

    One shared connection is guarded by a lock for in-process serialization;
    cross-process writes converge through SQLite locking and a bounded
    ``busy_timeout``. Transactions cover only database work, never model
    calls or file parsing.
    """

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        """Hold the resolved path and the shared migrated connection."""

        self.path = path
        self._connection = connection
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: str | Path | None = None) -> _StateDatabase:
        """Open or create the application state database.

        Args:
            path (`str | Path | None`):
                Test-injectable database path; defaults to
                ``~/.config/cli-agent/state.sqlite3``.

        Returns:
            A migrated database adapter owning one shared connection.

        Raises:
            ValueError: If the application directory or database file cannot
                be created with the required permissions.
        """

        database_path = (
            Path(path).expanduser() if path is not None else _default_state_db_path()
        )
        _prepare_state_path(database_path)
        connection = sqlite3.connect(
            database_path,
            timeout=_BUSY_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
        database = cls(database_path, connection)
        database._migrate()
        return database

    def close(self) -> None:
        """Close the shared connection idempotently."""

        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield the shared connection inside one short transaction.

        The transaction commits on normal exit and rolls back on any
        exception, so model calls and file parsing never sit inside it.
        """

        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _migrate(self) -> None:
        with self._lock:
            (version,) = self._connection.execute("PRAGMA user_version").fetchone()
            for target, script in enumerate(_MIGRATIONS, start=1):
                if target <= version:
                    continue
                self._connection.execute("BEGIN")
                try:
                    for statement in _split_statements(script):
                        self._connection.execute(statement)
                    self._connection.execute(f"PRAGMA user_version = {target}")
                    self._connection.commit()
                except BaseException:
                    self._connection.rollback()
                    raise


def _prepare_state_path(path: Path) -> None:
    """Create the application directory and database with private permissions."""

    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"cannot create application state directory: {path.parent}"
        ) from exc
    _ensure_real_directory(path.parent, label="application state directory")
    _ensure_real_file(path, label="application state database")


def _default_state_db_path() -> Path:
    """Resolve the default per-user application state database path."""

    return Path.home() / ".config" / "cli-agent" / "state.sqlite3"


def _split_statements(script: str) -> tuple[str, ...]:
    """Split one migration script into non-empty executable statements."""

    return tuple(
        statement.strip() for statement in script.split(";") if statement.strip()
    )
