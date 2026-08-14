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


def _open(path: Path) -> tuple[_StateDatabase, _SessionHistory]:
    database = _StateDatabase.open(path)
    return database, _SessionHistory(database)


def test_migration_creates_durable_schema_and_upgrades_user_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    database = _StateDatabase.open(path)
    database.close()

    connection = sqlite3.connect(path)
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    connection.close()

    assert version == 3
    assert {
        "sessions",
        "session_journal",
        "session_usage_records",
        "session_context_snapshots",
    } <= names
    assert "session_messages" not in names

    reopened = _StateDatabase.open(path)
    reopened.close()


def test_begin_session_reports_existing_id_and_keeps_first_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    database, history = _open(path)
    created = history.begin_session("s1", "/workspace", '{"blocks": []}')
    reused = history.begin_session(
        "s1",
        "/other",
        '{"blocks": [{"type": "text", "text": "x"}]}',
    )

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT workspace_id, config, revision FROM sessions WHERE session_id = 's1'"
    ).fetchall()
    connection.close()

    assert created is True
    assert reused is False
    assert [(row[0], row[2]) for row in rows] == [("/workspace", 0)]
    assert json.loads(rows[0][1]) == {
        "schema_version": 1,
        "system_prompt": '{"blocks": []}',
    }
    database.close()


def test_append_stores_ordered_journal_entries_with_serialized_payloads(
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
        "SELECT revision, role, payload, created_at FROM session_journal "
        "WHERE session_id = 's1' ORDER BY revision"
    ).fetchall()
    (revision,) = connection.execute(
        "SELECT revision FROM sessions WHERE session_id = 's1'"
    ).fetchone()
    connection.close()

    assert [(row[0], row[1]) for row in rows] == [
        (1, "user"),
        (2, "assistant"),
        (3, "tool_result"),
    ]
    assert revision == 3
    assert json.loads(rows[0][2]) == {
        "schema_version": 1,
        "role": "user",
        "blocks": [{"type": "text", "text": "hello"}],
    }
    assert json.loads(rows[1][2]) == {
        "schema_version": 1,
        "role": "assistant",
        "blocks": [
            {"type": "text", "text": "hi"},
            {
                "type": "tool_call",
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "ls"},
            },
        ],
    }
    assert json.loads(rows[2][2]) == {
        "schema_version": 1,
        "role": "tool_result",
        "results": [{"call_id": "c1", "output": {"ok": True}, "error": None}],
    }
    timestamps = [datetime.fromisoformat(row[3]) for row in rows]
    assert timestamps == sorted(timestamps)
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
    (count,) = connection.execute("SELECT COUNT(*) FROM session_journal").fetchone()
    connection.close()
    assert count == 0
    database.close()


def test_append_to_unknown_session_reports_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    received: list[RuntimeDiagnostic] = []
    database = _StateDatabase.open(path)
    history = _SessionHistory(database, on_diagnostic=received.append)

    history.append("missing", UserMessage.text("hello"))

    assert [diagnostic.detail["operation"] for diagnostic in received] == ["append"]
    connection = sqlite3.connect(path)
    (count,) = connection.execute("SELECT COUNT(*) FROM session_journal").fetchone()
    connection.close()
    assert count == 0
    database.close()
