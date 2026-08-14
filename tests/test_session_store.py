"""Repository contract tests for the fail-closed SessionStore."""

import json
import threading
from pathlib import Path

import pytest

from cli_agent.errors.session import (
    SessionConflictError,
    SessionCorruptedError,
    SessionNotFoundError,
    SessionPersistenceError,
)
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._session import SessionConfig, encode_journal_message
from cli_agent.runtime.model import (
    AssistantMessage,
    SystemMessage,
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


def _tool_result(call_id: str) -> ToolResultMessage:
    return ToolResultMessage(
        content=(ToolResult(call_id=call_id, output={"ok": True}),)
    )


def _journal_rows(database: _StateDatabase, session_id: str) -> list[tuple]:
    with database.transaction() as connection:
        return connection.execute(
            "SELECT revision, role, payload FROM session_journal "
            "WHERE session_id = ? ORDER BY revision",
            (session_id,),
        ).fetchall()


def _revision(database: _StateDatabase, session_id: str) -> int:
    with database.transaction() as connection:
        (revision,) = connection.execute(
            "SELECT revision FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return revision


def _corruption_reason(raised) -> str:
    assert isinstance(raised.value, SessionCorruptedError)
    reason = raised.value.details["reason"]
    assert isinstance(reason, str)
    return reason


def test_create_and_load_round_trip_empty_session(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    created = store.create("s1", "/workspace", _config())

    loaded, messages = store.load("s1")

    assert loaded == created
    assert loaded.revision == 0
    assert loaded.archived_at is None
    assert loaded.config == _config()
    assert messages == ()
    database.close()


def test_create_duplicate_conflicts_and_keeps_first_record(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())

    with pytest.raises(SessionConflictError) as raised:
        store.create("s1", "/other", _config())

    assert raised.value.code == "session_conflict"
    assert raised.value.details == {"session_id": "s1"}
    with database.transaction() as connection:
        (workspace_id,) = connection.execute(
            "SELECT workspace_id FROM sessions WHERE session_id = 's1'"
        ).fetchone()
    assert workspace_id == "/workspace"
    database.close()


def test_list_returns_sessions_in_creation_order(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    assert store.list() == ()

    store.create("s1", "/workspace", _config())
    store.create("s2", "/other", _config())

    sessions = store.list()

    assert tuple(session.session_id for session in sessions) == ("s1", "s2")
    assert all(session.revision == 0 for session in sessions)
    database.close()


def test_load_missing_session_fails_closed(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")

    with pytest.raises(SessionNotFoundError) as raised:
        store.load("missing")

    assert raised.value.code == "session_not_found"
    assert raised.value.details == {"session_id": "missing"}
    database.close()


def test_sequential_appends_round_trip_original_messages(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    messages = (
        _user("hello"),
        AssistantMessage(
            content=(
                TextBlock(text="hi"),
                ToolCall(call_id="c1", name="exec", arguments={"command": "ls"}),
            )
        ),
        _tool_result("c1"),
    )

    revisions = tuple(
        store.append("s1", message, expected_revision=revision)
        for revision, message in enumerate(messages)
    )

    assert revisions == (1, 2, 3)
    loaded, decoded = store.load("s1")
    assert loaded.revision == 3
    assert decoded == messages
    assert [row[0] for row in _journal_rows(database, "s1")] == [1, 2, 3]
    database.close()


def test_stale_append_conflicts_without_writing(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("first"), expected_revision=0)

    with pytest.raises(SessionConflictError) as raised:
        store.append("s1", _assistant("stale"), expected_revision=0)

    assert raised.value.code == "session_conflict"
    assert raised.value.details == {
        "session_id": "s1",
        "expected_revision": 0,
        "actual_revision": 1,
    }
    assert _revision(database, "s1") == 1
    assert [row[0] for row in _journal_rows(database, "s1")] == [1]
    database.close()


def test_append_to_missing_session_fails_closed(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")

    with pytest.raises(SessionNotFoundError):
        store.append("missing", _user("hello"), expected_revision=0)

    assert _journal_rows(database, "missing") == []
    database.close()


def test_append_after_database_close_raises_persistence_error(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    database.close()

    with pytest.raises(SessionPersistenceError) as raised:
        store.append("s1", _user("hello"), expected_revision=0)

    assert raised.value.code == "session_persistence_failed"
    assert raised.value.details == {
        "operation": "append",
        "session_id": "s1",
        "exception_type": "ProgrammingError",
    }


def test_append_of_system_message_rejects_invariant(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())

    with pytest.raises(ValueError, match="SystemMessage"):
        store.append("s1", SystemMessage.text("internal"), expected_revision=0)

    assert _revision(database, "s1") == 0
    assert _journal_rows(database, "s1") == []
    database.close()


def test_load_rejects_corrupted_config_payload(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    with database.transaction() as connection:
        connection.execute(
            "UPDATE sessions SET config = '{broken' WHERE session_id = 's1'"
        )

    with pytest.raises(SessionCorruptedError) as raised:
        store.load("s1")

    assert "not valid JSON" in _corruption_reason(raised)
    database.close()


def test_load_rejects_unknown_config_schema_version(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    with database.transaction() as connection:
        connection.execute(
            "UPDATE sessions SET config = ? WHERE session_id = 's1'",
            (json.dumps({"schema_version": 99, "system_prompt": "x"}),),
        )

    with pytest.raises(SessionCorruptedError) as raised:
        store.load("s1")

    assert "schema version" in _corruption_reason(raised)
    database.close()


def test_load_rejects_corrupted_journal_payload(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE session_journal SET payload = '{broken' "
            "WHERE session_id = 's1' AND revision = 1"
        )

    with pytest.raises(SessionCorruptedError) as raised:
        store.load("s1")

    assert "not valid JSON" in _corruption_reason(raised)
    database.close()


def test_load_rejects_role_payload_disagreement(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("hello"), expected_revision=0)
    _, assistant_payload = encode_journal_message(_assistant("hi"))
    with database.transaction() as connection:
        connection.execute(
            "UPDATE session_journal SET payload = ? "
            "WHERE session_id = 's1' AND revision = 1",
            (assistant_payload,),
        )

    with pytest.raises(SessionCorruptedError) as raised:
        store.load("s1")

    assert "disagrees with row role" in _corruption_reason(raised)
    database.close()


def test_load_rejects_revision_gap(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("first"), expected_revision=0)
    store.append("s1", _assistant("second"), expected_revision=1)
    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM session_journal WHERE session_id = 's1' AND revision = 1"
        )

    with pytest.raises(SessionCorruptedError) as raised:
        store.load("s1")

    assert "revision gap" in _corruption_reason(raised)
    database.close()


def test_load_rejects_journal_ahead_of_session_frontier(tmp_path: Path) -> None:
    database, store = _open(tmp_path / "state.sqlite3")
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("first"), expected_revision=0)
    with database.transaction() as connection:
        connection.execute("UPDATE sessions SET revision = 0 WHERE session_id = 's1'")

    with pytest.raises(SessionCorruptedError) as raised:
        store.load("s1")

    assert "frontier mismatch" in _corruption_reason(raised)
    database.close()


def test_failed_append_rolls_back_revision_and_journal(tmp_path: Path) -> None:
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

    with pytest.raises(SessionCorruptedError) as raised:
        store.append("s1", _assistant("attempted"), expected_revision=1)

    assert "already exists" in _corruption_reason(raised)
    assert _revision(database, "s1") == 1
    rows = _journal_rows(database, "s1")
    assert [row[0] for row in rows] == [1, 2]
    assert json.loads(rows[1][2])["blocks"][0]["text"] == "pre-existing"
    database.close()


def test_close_and_reopen_continues_appending(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    database, store = _open(path)
    store.create("s1", "/workspace", _config())
    store.append("s1", _user("first"), expected_revision=0)
    database.close()

    reopened, store = _open(path)
    loaded, messages = store.load("s1")

    assert loaded.revision == 1
    assert messages == (_user("first"),)
    assert store.append("s1", _assistant("second"), expected_revision=1) == 2
    assert store.load("s1")[0].revision == 2
    reopened.close()


def test_two_stores_conflict_on_same_expected_revision(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    database_a, store_a = _open(path)
    database_b, store_b = _open(path)
    store_a.create("s1", "/workspace", _config())
    store_a.append("s1", _user("first"), expected_revision=0)

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []

    def append_a() -> None:
        barrier.wait()
        try:
            outcomes.append(
                (
                    "a",
                    store_a.append("s1", _assistant("from a"), expected_revision=1),
                )
            )
        except SessionConflictError as exc:
            outcomes.append(("a", exc))

    def append_b() -> None:
        barrier.wait()
        try:
            outcomes.append(
                (
                    "b",
                    store_b.append("s1", _assistant("from b"), expected_revision=1),
                )
            )
        except SessionConflictError as exc:
            outcomes.append(("b", exc))

    threads = [threading.Thread(target=append_a), threading.Thread(target=append_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [value for _, value in outcomes if isinstance(value, int)]
    conflicts = [
        value for _, value in outcomes if isinstance(value, SessionConflictError)
    ]
    assert successes == [2]
    assert len(conflicts) == 1
    assert conflicts[0].details == {
        "session_id": "s1",
        "expected_revision": 1,
        "actual_revision": 2,
    }
    loaded, messages = store_a.load("s1")
    assert loaded.revision == 2
    assert messages[0] == _user("first")
    assert messages[1] in (_assistant("from a"), _assistant("from b"))
    database_a.close()
    database_b.close()
