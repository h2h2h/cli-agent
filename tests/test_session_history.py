import json
import sqlite3
from datetime import datetime
from pathlib import Path

from cli_agent.runtime._database.session_history import _SessionHistory
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

_V1_LIBRARY_TABLE = """CREATE TABLE library_summary_cache (
    fingerprint TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL
        CHECK (subject_kind IN ('file', 'directory')),
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL
)"""


def _open(path: Path) -> tuple[_StateDatabase, _SessionHistory]:
    database = _StateDatabase.open(path)
    return database, _SessionHistory(database)


def test_migration_creates_tables_and_upgrades_user_version(tmp_path: Path) -> None:
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
    assert {"sessions", "session_messages"} <= names
    connection.close()

    reopened = _StateDatabase.open(path)
    reopened.close()


def test_upgrade_from_version_1_preserves_library_summaries(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(_V1_LIBRARY_TABLE)
    connection.execute(
        "INSERT INTO library_summary_cache VALUES (?, ?, ?, ?, ?)",
        (
            "fp-1",
            "file",
            "kept summary",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    database = _StateDatabase.open(path)
    with database.transaction() as upgraded:
        (version,) = upgraded.execute("PRAGMA user_version").fetchone()
        assert version == 2
        (summary,) = upgraded.execute(
            "SELECT summary FROM library_summary_cache WHERE fingerprint = 'fp-1'"
        ).fetchone()
        assert summary == "kept summary"
    database.close()


def test_begin_session_keeps_first_record_on_reuse(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    database, history = _open(path)
    history.begin_session("s1", "/workspace", '{"blocks": []}')
    history.begin_session("s1", "/other", '{"blocks": [{"type": "text", "text": "x"}]}')

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT workspace, system_prompt FROM sessions WHERE session_id = 's1'"
    ).fetchall()
    connection.close()

    assert rows == [("/workspace", '{"blocks": []}')]
    database.close()


def test_append_stores_ordered_messages_with_serialized_payloads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    database, history = _open(path)
    history.begin_session("s1", "/workspace", '{"blocks": []}')
    history.append("s1", UserMessage.text("hello"))
    history.append(
        "s1",
        AssistantMessage(
            content=(
                TextBlock(text="hi"),
                ToolCall(call_id="c1", name="exec", arguments={"command": "ls"}),
            )
        ),
    )
    history.append(
        "s1",
        ToolResultMessage(content=(ToolResult(call_id="c1", output={"ok": True}),)),
    )

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT seq, role, payload, created_at FROM session_messages "
        "WHERE session_id = 's1' ORDER BY seq"
    ).fetchall()
    connection.close()

    assert [(row[0], row[1]) for row in rows] == [
        (0, "user"),
        (1, "assistant"),
        (2, "tool_result"),
    ]
    assert json.loads(rows[0][2]) == {
        "blocks": [{"type": "text", "text": "hello"}]
    }
    assert json.loads(rows[1][2]) == {
        "blocks": [
            {"type": "text", "text": "hi"},
            {
                "type": "tool_call",
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "ls"},
            },
        ]
    }
    assert json.loads(rows[2][2]) == {
        "results": [{"call_id": "c1", "output": {"ok": True}, "error": None}]
    }
    timestamps = [datetime.fromisoformat(row[3]) for row in rows]
    assert timestamps == sorted(timestamps)
    database.close()


def test_close_session_stamps_closed_at(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    database, history = _open(path)
    history.begin_session("s1", "/workspace", '{"blocks": []}')
    history.close_session("s1")

    connection = sqlite3.connect(path)
    (closed_at,) = connection.execute(
        "SELECT closed_at FROM sessions WHERE session_id = 's1'"
    ).fetchone()
    connection.close()

    assert datetime.fromisoformat(closed_at).tzinfo is not None
    database.close()


def test_append_after_database_close_reports_diagnostic_without_raising(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    received: list[RuntimeDiagnostic] = []
    database = _StateDatabase.open(path)
    history = _SessionHistory(database, on_diagnostic=received.append)
    history.begin_session("s1", "/workspace", '{"blocks": []}')
    database.close()

    history.append("s1", UserMessage.text("hello"))

    assert len(received) == 1
    assert received[0].kind == "session_history.write_failed"
    assert received[0].detail["session_id"] == "s1"
    assert received[0].detail["operation"] == "append"
    assert "closed database" in str(received[0].detail["error"])


def test_append_of_system_message_reports_diagnostic_without_raising(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    received: list[RuntimeDiagnostic] = []
    database = _StateDatabase.open(path)
    history = _SessionHistory(database, on_diagnostic=received.append)
    history.begin_session("s1", "/workspace", '{"blocks": []}')

    history.append("s1", SystemMessage.text("internal"))

    assert [diagnostic.kind for diagnostic in received] == [
        "session_history.write_failed"
    ]
    connection = sqlite3.connect(path)
    (count,) = connection.execute("SELECT COUNT(*) FROM session_messages").fetchone()
    connection.close()
    assert count == 0
    database.close()
