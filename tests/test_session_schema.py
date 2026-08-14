"""Schema, migration, and data-model tests for durable sessions."""

import json
import sqlite3
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._session import (
    CONTEXT_SNAPSHOT_SCHEMA_VERSION,
    ContextSnapshot,
    JournalEntry,
    ModelCallUsage,
    Session,
    SessionConfig,
    decode_journal_message,
    encode_journal_message,
    serialize_system_prompt,
)
from cli_agent.runtime.model import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

_NOW = "2026-01-01T00:00:00+00:00"


def _table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    connection.close()
    return names


def _insert_session(
    database: _StateDatabase,
    session_id: str = "s1",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions "
            "(session_id, workspace_id, revision, config, created_at, "
            "updated_at) VALUES (?, ?, 0, ?, ?, ?)",
            (session_id, "ws_1", _config_json(), _NOW, _NOW),
        )


def _insert_journal_row(
    database: _StateDatabase,
    session_id: str,
    revision: int,
    role: str = "user",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO session_journal "
            "(session_id, revision, role, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, revision, role, _journal_payload(role), _NOW),
        )
        connection.execute(
            "UPDATE sessions SET revision = ? WHERE session_id = ?",
            (revision, session_id),
        )


def _insert_usage_row(
    database: _StateDatabase,
    model_call_id: str,
    *,
    session_id: str = "s1",
    purpose: str = "agent",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO session_usage_records "
            "(model_call_id, session_id, purpose, input_tokens, "
            "output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                model_call_id,
                session_id,
                purpose,
                input_tokens,
                output_tokens,
                _NOW,
            ),
        )


def _insert_snapshot_row(
    database: _StateDatabase,
    session_id: str = "s1",
    source_revision: int = 0,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO session_context_snapshots "
            "(session_id, source_revision, derivation_version, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, source_revision, "v1", "{}", _NOW),
        )


def _config_json() -> str:
    return SessionConfig(system_prompt='{"blocks": []}').to_json()


def _journal_payload(role: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "role": role,
            "blocks": [{"type": "text", "text": "x"}],
        }
    )


def test_migration_creates_durable_session_tables(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    database = _StateDatabase.open(path)
    database.close()

    connection = sqlite3.connect(path)
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    connection.close()

    assert version == 3
    assert {
        "sessions",
        "session_journal",
        "session_usage_records",
        "session_context_snapshots",
    } <= _table_names(path)
    assert "session_messages" not in _table_names(path)


def test_migration_is_idempotent_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    database = _StateDatabase.open(path)
    _insert_session(database)
    _insert_journal_row(database, "s1", 1)
    database.close()

    reopened = _StateDatabase.open(path)
    with reopened.transaction() as connection:
        (version,) = connection.execute("PRAGMA user_version").fetchone()
        rows = connection.execute(
            "SELECT revision, role, payload FROM session_journal "
            "WHERE session_id = 's1'"
        ).fetchall()
    reopened.close()

    assert version == 3
    assert rows == [(1, "user", _journal_payload("user"))]


def test_journal_revision_is_unique_per_session(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")
    _insert_session(database)
    _insert_journal_row(database, "s1", 1)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_journal_row(database, "s1", 1)
    database.close()


def test_journal_rejects_zero_revision_and_unknown_role(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")
    _insert_session(database)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_journal_row(database, "s1", 0)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_journal_row(database, "s1", 1, role="system")
    database.close()


def test_journal_requires_existing_session(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")
    _insert_session(database)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_journal_row(database, "missing", 1)
    database.close()


def test_usage_model_call_id_is_globally_unique(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")
    _insert_session(database)
    _insert_session(database, "s2")
    _insert_usage_row(database, "mc_1")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_usage_row(database, "mc_1", session_id="s2")
    database.close()


def test_usage_rejects_invalid_purpose_and_negative_tokens(
    tmp_path: Path,
) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")
    _insert_session(database)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_usage_row(database, "mc_purpose", purpose="summary")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_usage_row(database, "mc_tokens", input_tokens=-1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_usage_row(database, "mc_missing_session", session_id="nope")
    database.close()


def test_snapshot_anchor_and_session_uniqueness(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")
    _insert_session(database)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_snapshot_row(database, source_revision=-1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_snapshot_row(database, session_id="missing")
    _insert_snapshot_row(database, source_revision=2)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_snapshot_row(database, source_revision=4)
    database.close()


def test_archive_is_metadata_only_and_delete_cascades(tmp_path: Path) -> None:
    database = _StateDatabase.open(tmp_path / "state.sqlite3")
    _insert_session(database)
    _insert_journal_row(database, "s1", 1)
    _insert_journal_row(database, "s1", 2, role="assistant")
    _insert_usage_row(database, "mc_1")
    _insert_snapshot_row(database, source_revision=2)

    def counts() -> tuple[int, int, int]:
        with database.transaction() as connection:
            journal = connection.execute(
                "SELECT COUNT(*) FROM session_journal WHERE session_id = 's1'"
            ).fetchone()[0]
            usage = connection.execute(
                "SELECT COUNT(*) FROM session_usage_records WHERE session_id = 's1'"
            ).fetchone()[0]
            snapshots = connection.execute(
                "SELECT COUNT(*) FROM session_context_snapshots WHERE session_id = 's1'"
            ).fetchone()[0]
        return journal, usage, snapshots

    assert counts() == (2, 1, 1)

    with database.transaction() as connection:
        connection.execute(
            "UPDATE sessions SET archived_at = ? WHERE session_id = 's1'",
            (_NOW,),
        )
        (archived_at,) = connection.execute(
            "SELECT archived_at FROM sessions WHERE session_id = 's1'"
        ).fetchone()
        assert archived_at is not None
    assert counts() == (2, 1, 1)

    with database.transaction() as connection:
        connection.execute(
            "UPDATE sessions SET archived_at = NULL WHERE session_id = 's1'"
        )
        (archived_at,) = connection.execute(
            "SELECT archived_at FROM sessions WHERE session_id = 's1'"
        ).fetchone()
        assert archived_at is None
    assert counts() == (2, 1, 1)

    with database.transaction() as connection:
        connection.execute("DELETE FROM sessions WHERE session_id = 's1'")
        (remaining,) = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = 's1'"
        ).fetchone()
    assert remaining == 0
    assert counts() == (0, 0, 0)
    database.close()


def test_session_models_are_frozen_pure_data() -> None:
    for model in (
        Session,
        SessionConfig,
        JournalEntry,
        ContextSnapshot,
        ModelCallUsage,
    ):
        instance = model(**{field.name: None for field in fields(model)})  # type: ignore[arg-type]
        with pytest.raises(FrozenInstanceError):
            setattr(instance, fields(model)[0].name, object())

    session = Session(
        session_id="s1",
        workspace_id="ws_1",
        revision=3,
        config=SessionConfig(system_prompt="{}"),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        archived_at=None,
    )
    assert session.revision == 3
    assert session.archived_at is None


def test_session_config_round_trip() -> None:
    config = SessionConfig(system_prompt='{"blocks": []}')

    decoded = SessionConfig.from_json(config.to_json())

    assert decoded == config
    document = json.loads(config.to_json())
    assert document["schema_version"] == 1


def test_session_config_rejects_unknown_versions() -> None:
    with pytest.raises(ValueError, match="schema version"):
        SessionConfig.from_json('{"schema_version": 99, "system_prompt": "x"}')
    with pytest.raises(ValueError, match="not valid JSON"):
        SessionConfig.from_json("{broken")
    with pytest.raises(ValueError, match="misses system_prompt"):
        SessionConfig.from_json('{"schema_version": 1}')


def test_journal_payload_round_trips_every_role() -> None:
    messages = (
        UserMessage.text("hello"),
        AssistantMessage(
            content=(
                TextBlock(text="hi"),
                ToolCall(call_id="c1", name="exec", arguments={"command": "ls"}),
            )
        ),
        ToolResultMessage(
            content=(
                ToolResult(call_id="c1", output={"ok": True}),
                ToolResult(call_id="c2", error={"code": "failed"}),
            )
        ),
    )

    for message in messages:
        role, payload = encode_journal_message(message)
        assert json.loads(payload)["schema_version"] == 1
        assert decode_journal_message(role, payload) == message


def test_journal_encode_rejects_system_messages() -> None:
    with pytest.raises(ValueError, match="SystemMessage"):
        encode_journal_message(SystemMessage.text("internal"))


def test_journal_decode_fails_closed() -> None:
    _, payload = encode_journal_message(UserMessage.text("hi"))

    with pytest.raises(ValueError, match="schema version"):
        decode_journal_message(
            "user",
            payload.replace('"schema_version": 1', '"schema_version": 99'),
        )
    with pytest.raises(ValueError, match="disagrees with row role"):
        decode_journal_message("assistant", payload)
    with pytest.raises(ValueError, match="unknown journal role"):
        decode_journal_message(
            "system",
            '{"schema_version": 1, "role": "system", "blocks": []}',
        )
    with pytest.raises(ValueError, match="not valid JSON"):
        decode_journal_message("user", "{broken")
    with pytest.raises(ValueError, match="misses blocks"):
        decode_journal_message(
            "user",
            '{"schema_version": 1, "role": "user"}',
        )


def test_context_snapshot_round_trip() -> None:
    snapshot = ContextSnapshot(
        session_id="s1",
        source_revision=3,
        summary="earlier discussion",
        context=(
            UserMessage.text("hello"),
            AssistantMessage.text("hi"),
            ToolResultMessage(content=(ToolResult(call_id="c1", output={"ok": True}),)),
        ),
        derivation_version="v1",
    )

    decoded = ContextSnapshot.from_json(
        snapshot.to_json(),
        session_id="s1",
        source_revision=3,
        derivation_version="v1",
    )

    assert decoded == snapshot
    document = json.loads(snapshot.to_json())
    assert document["schema_version"] == CONTEXT_SNAPSHOT_SCHEMA_VERSION


def test_context_snapshot_without_summary_round_trip() -> None:
    snapshot = ContextSnapshot(
        session_id="s1",
        source_revision=0,
        summary=None,
        context=(),
        derivation_version="v1",
    )

    decoded = ContextSnapshot.from_json(
        snapshot.to_json(),
        session_id="s1",
        source_revision=0,
        derivation_version="v1",
    )

    assert decoded == snapshot


def test_context_snapshot_rejects_unknown_versions() -> None:
    with pytest.raises(ValueError, match="schema version"):
        ContextSnapshot.from_json(
            '{"schema_version": 99}',
            session_id="s1",
            source_revision=0,
            derivation_version="v1",
        )
    with pytest.raises(ValueError, match="misses context"):
        ContextSnapshot.from_json(
            '{"schema_version": 1, "summary": null}',
            session_id="s1",
            source_revision=0,
            derivation_version="v1",
        )


def test_serialize_system_prompt_keeps_audit_form() -> None:
    prompt = serialize_system_prompt(SystemMessage.text("You are cli-agent"))

    assert json.loads(prompt) == {
        "blocks": [{"type": "text", "text": "You are cli-agent"}]
    }
