import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from cli_agent.runtime._database.state import _default_state_db_path, _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache


def _cache(path: Path) -> _SummaryCache:
    return _SummaryCache(_StateDatabase.open(path))


def test_migration_creates_table_and_reopens_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"

    database = _StateDatabase.open(path)
    database.close()

    connection = sqlite3.connect(path)
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    assert version == 2
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "library_summary_cache" in names
    connection.close()

    reopened = _StateDatabase.open(path)
    reopened.close()


def test_creation_uses_0700_directory_and_0600_database(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.sqlite3"

    database = _StateDatabase.open(path)
    database.close()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_paths_keep_their_permissions(tmp_path: Path) -> None:
    directory = tmp_path / "config"
    directory.mkdir(mode=0o755)
    path = directory / "state.sqlite3"
    path.touch()
    os.chmod(path, 0o644)

    database = _StateDatabase.open(path)
    database.close()

    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_cache_returns_only_hits(tmp_path: Path) -> None:
    cache = _cache(tmp_path / "state.sqlite3")
    cache.upsert("fp-1", "file", "summary one")
    cache.upsert("fp-2", "directory", "summary two")

    hits = cache.get(("fp-1", "fp-3", "fp-2"))

    assert hits == {"fp-1": "summary one", "fp-2": "summary two"}
    cache.close()


def test_get_with_no_fingerprints_returns_empty(tmp_path: Path) -> None:
    cache = _cache(tmp_path / "state.sqlite3")

    assert cache.get(()) == {}

    cache.close()


def test_summary_survives_new_runtime_instance(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"

    first = _cache(path)
    first.upsert("fp", "file", "stable summary")
    first.close()

    second = _cache(path)
    assert second.get(("fp",)) == {"fp": "stable summary"}
    second.close()


def test_upsert_keeps_single_row_and_refreshes_last_used_at(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    cache = _cache(path)
    cache.upsert("fp", "file", "first summary")
    cache.upsert("fp", "file", "second summary")

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT summary, created_at, last_used_at "
        "FROM library_summary_cache WHERE fingerprint = 'fp'"
    ).fetchall()
    connection.close()

    assert len(rows) == 1
    summary, created_at, last_used_at = rows[0]
    assert summary == "first summary"
    assert last_used_at > created_at
    cache.close()


def test_concurrent_upserts_converge_to_one_row(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first = _cache(path)
    second = _cache(path)
    errors: list[BaseException] = []

    def write(cache: _SummaryCache, summary: str) -> None:
        try:
            for _ in range(20):
                cache.upsert("shared-fp", "file", summary)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(first, "from-a")),
        threading.Thread(target=write, args=(second, "from-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    connection = sqlite3.connect(path)
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM library_summary_cache WHERE fingerprint = 'shared-fp'"
    ).fetchone()
    connection.close()

    assert errors == []
    assert count == 1
    first.close()
    second.close()


def test_upsert_rejects_unknown_subject_kind(tmp_path: Path) -> None:
    cache = _cache(tmp_path / "state.sqlite3")

    with pytest.raises(ValueError, match="unknown subject kind"):
        cache.upsert("fp", "table", "invalid subject kind")

    assert cache.get(("fp",)) == {}
    cache.close()


def test_transaction_rolls_back_failed_work(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")

    with pytest.raises(sqlite3.OperationalError):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO library_summary_cache "
                "(fingerprint, subject_kind, summary, created_at, last_used_at) "
                "VALUES ('fp', 'file', 'partial', 't', 't')"
            )
            connection.execute("SELECT * FROM missing_table")

    assert _SummaryCache(database).get(("fp",)) == {}
    database.close()


def test_database_schema_stores_only_cache_columns(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    _cache(path).close()

    connection = sqlite3.connect(path)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(library_summary_cache)")
    }
    connection.close()

    assert columns == {
        "fingerprint",
        "subject_kind",
        "summary",
        "created_at",
        "last_used_at",
    }


def test_busy_timeout_is_bounded(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")

    with database.transaction() as connection:
        (timeout_ms,) = connection.execute("PRAGMA busy_timeout").fetchone()

    assert timeout_ms == 5000
    database.close()


def test_default_database_path_is_user_config() -> None:
    assert _default_state_db_path() == (
        Path.home() / ".cli-agent" / "state.sqlite3"
    )
