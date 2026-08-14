"""Contract tests for usage records and context snapshots."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cli_agent.errors.session import (
    SessionConflictError,
    SessionCorruptedError,
    SessionNotFoundError,
)
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._session import (
    ContextSnapshot,
    ModelCallUsage,
    SessionConfig,
    encode_journal_message,
)
from cli_agent.runtime.model import (
    AssistantMessage,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


def _open(path: Path) -> tuple[_StateDatabase, SessionStore]:
    database = _StateDatabase.open(path)
    return database, SessionStore(database)


def _config() -> SessionConfig:
    return SessionConfig(system_prompt='{"blocks": []}')


def _user(text: str) -> UserMessage:
    return UserMessage.text(text)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage.text(text)


def _usage(
    model_call_id: str = "mc_1",
    *,
    session_id: str = "s1",
    purpose: str = "agent",
    input_tokens: int = 12,
    output_tokens: int = 7,
) -> ModelCallUsage:
    return ModelCallUsage(
        model_call_id=model_call_id,
        session_id=session_id,
        purpose=purpose,  # type: ignore[arg-type]
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        created_at=datetime.now(timezone.utc),
    )


def _snapshot(
    *,
    source_revision: int,
    summary: str | None = None,
) -> ContextSnapshot:
    return ContextSnapshot(
        session_id="s1",
        source_revision=source_revision,
        summary=summary,
        context=(),
        derivation_version="v1",
    )


def _session_revision(database: _StateDatabase, session_id: str) -> int:
    with database.transaction() as connection:
        (revision,) = connection.execute(
            "SELECT revision FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return revision


def _journal_rows(database: _StateDatabase, session_id: str) -> list[tuple]:
    with database.transaction() as connection:
        return connection.execute(
            "SELECT revision, role, payload FROM session_journal "
            "WHERE session_id = ? ORDER BY revision",
            (session_id,),
        ).fetchall()


def test_completion_commit_appends_message_and_usage_atomically(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)

    revision = store.append(
        "s1",
        _assistant("hi there"),
        expected_revision=1,
        usage=_usage(),
    )

    assert revision == 2
    _, messages = store.load("s1")
    assert messages == (_user("hello"), _assistant("hi there"))
    (record,) = store.load_usage_records("s1")
    assert record.model_call_id == "mc_1"
    assert record.session_id == "s1"
    assert record.purpose == "agent"
    assert (record.input_tokens, record.output_tokens) == (12, 7)
    assert store.usage_total("s1") == (12, 7)
    database.close()


def test_completion_retry_with_same_model_call_id_is_idempotent(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    message = _assistant("hi there")
    usage = _usage()
    assert store.append("s1", message, expected_revision=1, usage=usage) == 2

    retried = store.append("s1", message, expected_revision=1, usage=usage)
    retried_stale = store.append("s1", message, expected_revision=0, usage=usage)

    assert retried == 2
    assert retried_stale == 2
    _, messages = store.load("s1")
    assert messages == (_user("hello"), message)
    assert len(store.load_usage_records("s1")) == 1
    assert store.usage_total("s1") == (12, 7)
    database.close()


def test_duplicate_model_call_id_with_different_usage_conflicts(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.append(
        "s1",
        _assistant("first reply"),
        expected_revision=1,
        usage=_usage(),
    )

    with pytest.raises(SessionConflictError) as raised:
        store.append(
            "s1",
            _assistant("second reply"),
            expected_revision=2,
            usage=_usage(input_tokens=99),
        )

    assert raised.value.code == "session_conflict"
    assert raised.value.details == {"session_id": "s1", "model_call_id": "mc_1"}
    _, messages = store.load("s1")
    assert messages == (_user("hello"), _assistant("first reply"))
    assert store.usage_total("s1") == (12, 7)
    database.close()


def test_failed_completion_commit_rolls_back_message_and_usage(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("first"), expected_revision=0)
    _, pre_existing = encode_journal_message(_assistant("pre-existing"))
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO session_journal "
            "(session_id, revision, role, payload, created_at) "
            "VALUES ('s1', 2, 'assistant', ?, ?)",
            (pre_existing, "2026-01-01T00:00:00+00:00"),
        )

    with pytest.raises(SessionCorruptedError):
        store.append(
            "s1",
            _assistant("attempted"),
            expected_revision=1,
            usage=_usage(),
        )

    assert _session_revision(database, "s1") == 1
    rows = _journal_rows(database, "s1")
    assert [row[0] for row in rows] == [1, 2]
    assert json.loads(rows[1][2])["blocks"][0]["text"] == "pre-existing"
    assert store.load_usage_records("s1") == ()
    assert store.usage_total("s1") == (0, 0)
    database.close()


def test_usage_total_rebuilds_from_records(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.append(
        "s1",
        _assistant("a"),
        expected_revision=1,
        usage=_usage("mc_1", input_tokens=12, output_tokens=7),
    )
    store.append(
        "s1",
        _assistant("b"),
        expected_revision=2,
        usage=_usage("mc_2", input_tokens=30, output_tokens=11),
    )

    records = store.load_usage_records("s1")
    rebuilt = (
        sum(record.input_tokens for record in records),
        sum(record.output_tokens for record in records),
    )

    assert store.usage_total("s1") == rebuilt == (42, 18)
    assert store.load_usage_records("missing") == ()
    assert store.usage_total("missing") == (0, 0)
    database.close()


def test_usage_records_reject_corrupted_timestamp(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append(
        "s1",
        _assistant("a"),
        expected_revision=0,
        usage=_usage(),
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE session_usage_records SET created_at = 'not-a-time' "
            "WHERE model_call_id = 'mc_1'"
        )

    with pytest.raises(SessionCorruptedError):
        store.load_usage_records("s1")
    database.close()


def test_snapshot_hit_leaves_journal_delta_rebuildable(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("first"), expected_revision=0)
    store.append("s1", _assistant("first reply"), expected_revision=1)
    snapshot = _snapshot(source_revision=2, summary="first turn")
    store.save_snapshot(snapshot, expected_revision=2)
    assert _session_revision(database, "s1") == 2

    store.append("s1", _user("second"), expected_revision=2)
    store.append("s1", _assistant("second reply"), expected_revision=3)

    loaded = store.load_snapshot("s1", derivation_version="v1")
    _, messages = store.load("s1")

    assert loaded == snapshot
    assert messages[loaded.source_revision :] == (
        _user("second"),
        _assistant("second reply"),
    )
    database.close()


def test_save_snapshot_replaces_previous(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.save_snapshot(
        _snapshot(source_revision=1, summary="old"),
        expected_revision=1,
    )

    store.save_snapshot(
        _snapshot(source_revision=1, summary="new"),
        expected_revision=1,
    )

    loaded = store.load_snapshot("s1", derivation_version="v1")
    assert loaded is not None
    assert loaded.summary == "new"
    database.close()


def test_snapshot_derivation_version_mismatch_returns_unavailable(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.save_snapshot(_snapshot(source_revision=0), expected_revision=0)

    assert store.load_snapshot("s1", derivation_version="v2") is None
    database.close()


def test_corrupted_snapshot_returns_unavailable_and_keeps_journal(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.save_snapshot(_snapshot(source_revision=1), expected_revision=1)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE session_context_snapshots SET payload = '{broken' "
            "WHERE session_id = 's1'"
        )

    assert store.load_snapshot("s1", derivation_version="v1") is None
    _, messages = store.load("s1")
    assert messages == (_user("hello"),)
    database.close()


def test_snapshot_ahead_of_session_frontier_returns_unavailable(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.save_snapshot(_snapshot(source_revision=1), expected_revision=1)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE session_context_snapshots SET source_revision = 99 "
            "WHERE session_id = 's1'"
        )

    assert store.load_snapshot("s1", derivation_version="v1") is None
    database.close()


def test_snapshot_commit_records_compaction_usage_atomically(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    usage = _usage(
        "mc_compaction",
        purpose="compaction",
        input_tokens=50,
        output_tokens=9,
    )
    snapshot = _snapshot(source_revision=1)

    store.save_snapshot(snapshot, expected_revision=1, usage=usage)
    (record,) = store.load_usage_records("s1")

    assert record.model_call_id == "mc_compaction"
    assert record.purpose == "compaction"
    assert store.usage_total("s1") == (50, 9)

    store.save_snapshot(snapshot, expected_revision=1, usage=usage)
    assert len(store.load_usage_records("s1")) == 1
    assert store.load_snapshot("s1", derivation_version="v1") == snapshot
    database.close()


def test_invalidate_snapshot_removes_cache_only(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.save_snapshot(_snapshot(source_revision=1), expected_revision=1)

    store.invalidate_snapshot("s1")

    assert store.load_snapshot("s1", derivation_version="v1") is None
    _, messages = store.load("s1")
    assert messages == (_user("hello"),)
    assert _session_revision(database, "s1") == 1
    database.close()


def test_save_snapshot_with_stale_revision_conflicts(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)

    with pytest.raises(SessionConflictError) as raised:
        store.save_snapshot(_snapshot(source_revision=0), expected_revision=0)

    assert raised.value.details == {
        "session_id": "s1",
        "expected_revision": 0,
        "actual_revision": 1,
    }
    assert store.load_snapshot("s1", derivation_version="v1") is None
    database.close()


def test_save_snapshot_to_missing_session_fails_closed(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")

    with pytest.raises(SessionNotFoundError):
        store.save_snapshot(_snapshot(source_revision=0), expected_revision=0)
    database.close()


def test_save_snapshot_rejects_future_source_revision(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())

    with pytest.raises(ValueError, match="source revision"):
        store.save_snapshot(_snapshot(source_revision=5), expected_revision=0)
    database.close()


def test_snapshot_keeps_tool_result_context_round_trip(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.append(
        "s1",
        AssistantMessage(
            content=(
                TextBlock(text="inspecting"),
                ToolCall(call_id="c1", name="exec", arguments={"command": "ls"}),
            )
        ),
        expected_revision=1,
    )
    store.append(
        "s1",
        ToolResultMessage(content=(ToolResult(call_id="c1", output={"ok": True}),)),
        expected_revision=2,
    )
    snapshot = ContextSnapshot(
        session_id="s1",
        source_revision=3,
        summary="inspected",
        context=(_user("hello"),),
        derivation_version="v1",
    )

    store.save_snapshot(snapshot, expected_revision=3)

    loaded = store.load_snapshot("s1", derivation_version="v1")
    assert loaded == snapshot
    database.close()
