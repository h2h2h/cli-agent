"""Contract tests for session lifecycle and crash-frontier repair."""

import json
from pathlib import Path

import pytest

from cli_agent.errors.session import (
    SessionArchivedError,
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
    decode_journal_message,
)
from cli_agent.runtime.model import (
    AssistantMessage,
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


def _assistant_calls(*calls: ToolCall) -> AssistantMessage:
    return AssistantMessage(content=tuple(calls))


def _tool_call(call_id: str) -> ToolCall:
    return ToolCall(call_id=call_id, name="exec", arguments={"command": "ls"})


def _tool_result(*call_ids: str) -> ToolResultMessage:
    return ToolResultMessage(
        content=tuple(
            ToolResult(call_id=call_id, output={"ok": True}) for call_id in call_ids
        )
    )


def _journal_rows(database: _StateDatabase, session_id: str) -> list[tuple]:
    with database.transaction() as connection:
        return connection.execute(
            "SELECT revision, role, payload FROM session_journal "
            "WHERE session_id = ? ORDER BY revision",
            (session_id,),
        ).fetchall()


def _archived_at(database: _StateDatabase, session_id: str) -> str | None:
    with database.transaction() as connection:
        (value,) = connection.execute(
            "SELECT archived_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return value


def _count(database: _StateDatabase, table: str, session_id: str) -> int:
    with database.transaction() as connection:
        (count,) = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return count


def test_archive_and_unarchive_only_toggle_the_marker(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)

    store.archive("s1")
    assert _archived_at(database, "s1") is not None
    assert store.list() == ()
    assert tuple(s.session_id for s in store.list(include_archived=True)) == ("s1",)
    assert _count(database, "session_journal", "s1") == 1

    store.unarchive("s1")
    assert _archived_at(database, "s1") is None
    assert tuple(s.session_id for s in store.list()) == ("s1",)
    database.close()


def test_archive_and_unarchive_missing_session_fail_closed(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")

    with pytest.raises(SessionNotFoundError):
        store.archive("missing")
    with pytest.raises(SessionNotFoundError):
        store.unarchive("missing")
    database.close()


def test_delete_cascades_journal_usage_and_snapshot(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=1)
    store.save_snapshot(
        ContextSnapshot(
            session_id="s1",
            source_revision=2,
            summary=None,
            context=(),
            derivation_version="v1",
        ),
        expected_revision=2,
        usage=ModelCallUsage(
            model_call_id="mc_1",
            session_id="s1",
            purpose="compaction",
            input_tokens=1,
            output_tokens=1,
            created_at=None,  # type: ignore[arg-type]
        ),
    )

    store.delete("s1")

    assert _count(database, "sessions", "s1") == 0
    assert _count(database, "session_journal", "s1") == 0
    assert _count(database, "session_usage_records", "s1") == 0
    assert _count(database, "session_context_snapshots", "s1") == 0
    with pytest.raises(SessionNotFoundError):
        store.delete("s1")
    database.close()


def test_list_excludes_archived_sessions_by_default(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.create("s2", "/workspace", _config())
    store.archive("s2")

    assert tuple(s.session_id for s in store.list()) == ("s1",)
    assert tuple(s.session_id for s in store.list(include_archived=True)) == (
        "s1",
        "s2",
    )
    database.close()


def test_repair_appends_one_synthetic_result_per_unresolved_call(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.append(
        "s1",
        _assistant_calls(_tool_call("c1"), _tool_call("c2")),
        expected_revision=1,
    )

    revision = store.repair_interrupted_execution("s1", expected_revision=2)

    assert revision == 3
    loaded, messages = store.load("s1")
    assert loaded.revision == 3
    (repaired,) = messages[2:]
    assert isinstance(repaired, ToolResultMessage)
    assert [(result.call_id, result.error) for result in repaired.content] == [
        ("c1", None),
        ("c2", None),
    ]
    for result in repaired.content:
        assert result.output == {
            "code": "execution_interrupted",
            "message": (
                "The previous tool execution was interrupted. Its side "
                "effects are unknown. Inspect the workspace before retrying."
            ),
            "outcome": "unknown",
        }
    database.close()


def test_repair_skips_resolved_turns_and_repairs_only_the_frontier(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("first"), expected_revision=0)
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=1)
    store.append("s1", _tool_result("c1"), expected_revision=2)
    store.append("s1", _user("second"), expected_revision=3)
    store.append("s1", _assistant_calls(_tool_call("c2")), expected_revision=4)

    revision = store.repair_interrupted_execution("s1", expected_revision=5)

    assert revision == 6
    _, messages = store.load("s1")
    (repaired,) = messages[5:]
    assert isinstance(repaired, ToolResultMessage)
    assert [result.call_id for result in repaired.content] == ["c2"]
    database.close()


def test_repair_is_idempotent_and_respects_expected_revision(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=0)
    store.repair_interrupted_execution("s1", expected_revision=1)

    second = store.repair_interrupted_execution("s1", expected_revision=2)
    stale = store.repair_interrupted_execution("s1", expected_revision=1)

    assert second == 2
    assert stale == 2
    assert len(_journal_rows(database, "s1")) == 2
    database.close()


def test_repair_noop_on_resolved_frontier(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.append(
        "s1",
        AssistantMessage.text("done"),
        expected_revision=1,
    )

    revision = store.repair_interrupted_execution("s1", expected_revision=2)

    assert revision == 2
    assert len(_journal_rows(database, "s1")) == 2
    database.close()


def test_repair_rejects_archived_session(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=0)
    store.archive("s1")

    with pytest.raises(SessionArchivedError) as raised:
        store.repair_interrupted_execution("s1", expected_revision=1)

    assert raised.value.code == "session_archived"
    assert len(_journal_rows(database, "s1")) == 1
    database.close()


def test_repair_with_stale_revision_conflicts_without_writing(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=0)
    store.append("s1", _user("concurrent"), expected_revision=1)

    with pytest.raises(SessionConflictError) as raised:
        store.repair_interrupted_execution("s1", expected_revision=1)

    assert raised.value.details == {
        "session_id": "s1",
        "expected_revision": 1,
        "actual_revision": 2,
    }
    assert len(_journal_rows(database, "s1")) == 2
    database.close()


def test_repair_missing_session_fails_closed(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")

    with pytest.raises(SessionNotFoundError):
        store.repair_interrupted_execution("missing", expected_revision=0)
    database.close()


def test_repair_fails_closed_on_duplicate_tool_call_id(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=0)
    store.append("s1", _tool_result("c1"), expected_revision=1)
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=2)

    with pytest.raises(SessionCorruptedError) as raised:
        store.repair_interrupted_execution("s1", expected_revision=3)

    assert "duplicate tool call id" in raised.value.details["reason"]
    assert len(_journal_rows(database, "s1")) == 3
    database.close()


def test_repair_fails_closed_on_result_without_preceding_call(
    tmp_path: Path,
) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    store.append("s1", _tool_result("orphan"), expected_revision=1)

    with pytest.raises(SessionCorruptedError) as raised:
        store.repair_interrupted_execution("s1", expected_revision=2)

    assert "without preceding tool call" in raised.value.details["reason"]
    assert len(_journal_rows(database, "s1")) == 2
    database.close()


def test_synthetic_result_round_trips_through_journal_codec(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _assistant_calls(_tool_call("c1")), expected_revision=0)
    store.repair_interrupted_execution("s1", expected_revision=1)

    (_, role, payload) = _journal_rows(database, "s1")[1]

    message = decode_journal_message(role, payload)

    assert isinstance(message, ToolResultMessage)
    assert message == ToolResultMessage(
        content=(
            ToolResult(
                call_id="c1",
                output={
                    "code": "execution_interrupted",
                    "message": (
                        "The previous tool execution was interrupted. Its "
                        "side effects are unknown. Inspect the workspace "
                        "before retrying."
                    ),
                    "outcome": "unknown",
                },
            ),
        )
    )
    document = json.loads(payload)
    assert document["schema_version"] == 1
    database.close()
