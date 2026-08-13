"""Fingerprint-keyed summary cache over the application state database."""

from __future__ import annotations

from datetime import datetime, timezone

from cli_agent.runtime._database.state import _StateDatabase

_SUBJECT_KINDS = ("file", "directory")


class _SummaryCache:
    """Minimal persistent cache for successful Library summaries.

    Cache identity is the fingerprint alone; model names, provider adapters,
    and prompts are never part of the cache key or schema. Only successful
    summaries are stored: raw sources, parser output, credentials, pending
    jobs, and failures never enter the database.
    """

    def __init__(self, database: _StateDatabase) -> None:
        """Hold the state database adapter shared with the Runtime."""

        self._database = database

    def get(self, fingerprints: tuple[str, ...]) -> dict[str, str]:
        """Return summaries for the fingerprints with cache hits.

        Args:
            fingerprints (`tuple[str, ...]`):
                Discovered fingerprints to look up in one batch.

        Returns:
            A mapping from hit fingerprint to its cached summary. Hits also
            refresh ``last_used_at`` inside the same short transaction.
        """

        if not fingerprints:
            return {}
        placeholders = ", ".join("?" for _ in fingerprints)
        now = _utc_now()
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT fingerprint, summary FROM library_summary_cache "
                f"WHERE fingerprint IN ({placeholders})",
                fingerprints,
            ).fetchall()
            if rows:
                connection.execute(
                    "UPDATE library_summary_cache SET last_used_at = ? "
                    f"WHERE fingerprint IN ({placeholders})",
                    (now, *fingerprints),
                )
        return {fingerprint: summary for fingerprint, summary in rows}

    def upsert(self, fingerprint: str, subject_kind: str, summary: str) -> None:
        """Persist one successful summary, reusing any existing record.

        A concurrent process may win the row: ``INSERT OR IGNORE`` keeps the
        winning record and only refreshes ``last_used_at``, so repeated model
        calls for the same fingerprint converge to one stored summary.

        Args:
            fingerprint (`str`):
                The already domain-separated content fingerprint.
            subject_kind (`str`):
                Readable ``file`` or ``directory`` metadata for inspection.
            summary (`str`):
                The successful model summary to cache.

        Raises:
            ValueError: If ``subject_kind`` is not ``file`` or ``directory``.
        """

        if subject_kind not in _SUBJECT_KINDS:
            raise ValueError(f"unknown subject kind: {subject_kind}")
        now = _utc_now()
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO library_summary_cache "
                "(fingerprint, subject_kind, summary, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fingerprint, subject_kind, summary, now, now),
            )
            connection.execute(
                "UPDATE library_summary_cache SET last_used_at = ? "
                "WHERE fingerprint = ?",
                (now, fingerprint),
            )

    def close(self) -> None:
        """Close the underlying state database connection."""

        self._database.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
