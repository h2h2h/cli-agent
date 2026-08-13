"""Append-only session trace over the application state database.

Each session message is persisted as its own row in ``session_messages``
inside a short transaction, preserving the original content before
compaction. The adapter is write-only and failure-safe: every database
exception is swallowed and reported through ``on_diagnostic``, so a failing
trace never interrupts the agent loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeAlias

from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

_OnDiagnostic: TypeAlias = Callable[[RuntimeDiagnostic], None] | None


class _SessionHistory:
    """Persist one session's message trace in append-only row form.

    ``begin_session`` records session metadata once; ``append`` writes each
    message with a fresh per-session ``seq`` inside one short transaction;
    ``close_session`` stamps ``closed_at``. All database work swallows
    exceptions and reports ``session_history.write_failed`` through the
    injected diagnostic callback.
    """

    def __init__(
        self,
        database: _StateDatabase,
        on_diagnostic: _OnDiagnostic = None,
    ) -> None:
        """Hold the state database adapter shared with the Runtime."""

        self._database = database
        self._on_diagnostic = on_diagnostic

    def begin_session(
        self,
        session_id: str,
        workspace: str,
        system_prompt: str,
    ) -> None:
        """Record session metadata once, keeping the first row on reuse.

        Args:
            session_id (`str`): The Host-provided session identifier.
            workspace (`str`): The session's working directory.
            system_prompt (`str`): The serialized system message blocks.
        """

        self._write(
            operation="begin_session",
            session_id=session_id,
            sql=(
                "INSERT OR IGNORE INTO sessions "
                "(session_id, workspace, system_prompt, created_at) "
                "VALUES (?, ?, ?, ?)"
            ),
            params=(session_id, workspace, system_prompt, _utc_now()),
        )

    def append(self, session_id: str, message: ModelMessage) -> None:
        """Persist one message with a fresh ``seq`` inside a short transaction.

        Args:
            session_id (`str`): The session the message belongs to.
            message (`ModelMessage`): The message just appended to Context
                History; a ``SystemMessage`` is not a valid trace entry.
        """

        try:
            role, payload = _serialize_message(message)
            with self._database.transaction() as connection:
                (max_seq,) = connection.execute(
                    "SELECT COALESCE(MAX(seq), -1) FROM session_messages "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute(
                    "INSERT INTO session_messages "
                    "(session_id, seq, role, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, max_seq + 1, role, payload, _utc_now()),
                )
        except Exception as exc:
            self._report("append", session_id, exc)

    def close_session(self, session_id: str) -> None:
        """Stamp ``closed_at`` on the session's record when present."""

        self._write(
            operation="close_session",
            session_id=session_id,
            sql="UPDATE sessions SET closed_at = ? WHERE session_id = ?",
            params=(_utc_now(), session_id),
        )

    def _write(
        self,
        *,
        operation: str,
        session_id: str,
        sql: str,
        params: tuple[object, ...],
    ) -> None:
        try:
            with self._database.transaction() as connection:
                connection.execute(sql, params)
        except Exception as exc:
            self._report(operation, session_id, exc)

    def _report(self, operation: str, session_id: str, exc: Exception) -> None:
        if self._on_diagnostic is None:
            return
        self._on_diagnostic(
            RuntimeDiagnostic(
                kind="session_history.write_failed",
                message=(
                    f"session history {operation} failed; the agent continues "
                    "without persistence"
                ),
                detail={
                    "session_id": session_id,
                    "operation": operation,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        )


def serialize_system_prompt(system_message: SystemMessage) -> str:
    """Serialize a System Message to the ``sessions.system_prompt`` form.

    System Messages never enter ``session_messages``; this form is stored
    once per session so the trace can be interpreted without the model's
    current catalog state.
    """

    return _dump({"blocks": [_text_block(block) for block in system_message.content]})


def _serialize_message(message: ModelMessage) -> tuple[str, str]:
    """Return the ``(role, payload)`` row values for one message.

    The payload mirrors the ``model.py`` dataclass structure: ``blocks`` for
    user and assistant messages, ``results`` for tool result messages.
    """

    if isinstance(message, UserMessage):
        return "user", _dump(
            {"blocks": [_text_block(block) for block in message.content]}
        )
    if isinstance(message, AssistantMessage):
        return "assistant", _dump(
            {
                "blocks": [
                    _tool_call_block(block)
                    if isinstance(block, ToolCall)
                    else _text_block(block)
                    for block in message.content
                ]
            }
        )
    if isinstance(message, ToolResultMessage):
        return "tool_result", _dump(
            {
                "results": [
                    {
                        "call_id": result.call_id,
                        "output": result.output,
                        "error": result.error,
                    }
                    for result in message.content
                ]
            }
        )
    raise ValueError(f"cannot serialize {type(message).__name__} to session history")


def _text_block(block: TextBlock) -> dict[str, str]:
    return {"type": "text", "text": block.text}


def _tool_call_block(block: ToolCall) -> dict[str, object]:
    return {
        "type": "tool_call",
        "call_id": block.call_id,
        "name": block.name,
        "arguments": block.arguments,
    }


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
