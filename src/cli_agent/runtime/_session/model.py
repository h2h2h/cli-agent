"""Pure durable-data models for sessions: journal, config, snapshot, usage.

Session durability separates three domains with different consistency
contracts:

- ``session_journal`` rows are the canonical conversation truth; they are
  append-only and never rewritten or deleted by snapshots or compaction.
- ``session_usage_records`` rows are the accounting truth, deduplicated by
  ``model_call_id``.
- ``session_context_snapshots`` are rebuildable derived caches anchored to
  the journal revision they were derived from.

Every persisted JSON payload carries an explicit ``schema_version`` so
deserialization never depends on the implicit shape of private Python
dataclasses. All models are frozen data holding no runtime resources,
locks, tasks, or execution state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

SESSION_CONFIG_SCHEMA_VERSION = 1
JOURNAL_PAYLOAD_SCHEMA_VERSION = 1
CONTEXT_SNAPSHOT_SCHEMA_VERSION = 1

JournalRole: TypeAlias = Literal["user", "assistant", "tool_result"]
UsagePurpose: TypeAlias = Literal["agent", "compaction"]


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Stable per-session configuration persisted beside the session.

    The attach-time system prompt is stored for audit and debugging only;
    resume always reassembles the current system prompt instead of
    trusting this snapshot.

    Args:
        system_prompt (`str`): The serialized system-message blocks
            captured when the session was created.
    """

    system_prompt: str

    def to_json(self) -> str:
        """Serialize with the explicit session-config schema version."""

        return _dump(
            {
                "schema_version": SESSION_CONFIG_SCHEMA_VERSION,
                "system_prompt": self.system_prompt,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> SessionConfig:
        """Deserialize one session-config payload.

        Args:
            value (`str`): The persisted JSON payload.

        Returns:
            The decoded `SessionConfig`.

        Raises:
            ValueError: If the payload is not valid JSON, carries an
                unknown schema version, or misses required fields.
        """

        document = _load_object(value)
        _require_version(
            document,
            SESSION_CONFIG_SCHEMA_VERSION,
            label="session config",
        )
        system_prompt = document.get("system_prompt")
        if not isinstance(system_prompt, str):
            raise ValueError("session config payload misses system_prompt")
        return cls(system_prompt=system_prompt)


@dataclass(frozen=True, slots=True)
class Session:
    """One durable session's metadata row.

    The session owns no live runtime state; kernels, locks, and tasks
    belong to the Runtime. ``revision`` is the journal frontier: the
    revision of the last durably appended journal entry.

    Args:
        session_id (`str`): Host-visible session identifier.
        workspace_id (`str`): Stable logical workspace identity the
            session belongs to.
        revision (`int`): Last durably appended journal revision; ``0``
            for an empty session.
        config (`SessionConfig`): Stable configuration captured at
            session creation.
        created_at (`datetime`): Session creation time.
        updated_at (`datetime`): Last durable-write time.
        archived_at (`datetime | None`): User-explicit archive marker;
            ``None`` while the session is live.
    """

    session_id: str
    workspace_id: str
    revision: int
    config: SessionConfig
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One canonical journal row holding the original message payload.

    Args:
        revision (`int`): The entry's journal revision, unique and
            gap-free per session starting at ``1``.
        role (`JournalRole`): The message role stored beside the payload.
        payload (`str`): The schema-versioned serialized message payload.
        created_at (`datetime`): When the entry was durably appended.
    """

    revision: int
    role: JournalRole
    payload: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """A durable derived cache of the conversation projection.

    A snapshot never replaces the journal: it only accelerates resume.
    Invalid, corrupted, or version-mismatched snapshots are discarded and
    rebuilt from the raw journal.

    Args:
        session_id (`str`): The session the snapshot belongs to.
        source_revision (`int`): The journal revision the projection was
            derived from.
        summary (`str | None`): Optional compaction summary replacing
            older conversation turns.
        context (`tuple[ModelMessage, ...]`): The derived conversation
            projection; never contains the attach-time SystemMessage.
        derivation_version (`str`): Version of the derivation logic that
            produced this snapshot.
    """

    session_id: str
    source_revision: int
    summary: str | None
    context: tuple[ModelMessage, ...]
    derivation_version: str

    def to_json(self) -> str:
        """Serialize with the explicit snapshot payload schema version."""

        return _dump(
            {
                "schema_version": CONTEXT_SNAPSHOT_SCHEMA_VERSION,
                "summary": self.summary,
                "context": [
                    {"role": role, "payload": json.loads(payload)}
                    for role, payload in (
                        encode_journal_message(message) for message in self.context
                    )
                ],
            }
        )

    @classmethod
    def from_json(
        cls,
        value: str,
        *,
        session_id: str,
        source_revision: int,
        derivation_version: str,
    ) -> ContextSnapshot:
        """Deserialize one snapshot payload.

        Args:
            value (`str`): The persisted JSON payload.
            session_id (`str`): Row identity from the storing table.
            source_revision (`int`): Row anchor revision.
            derivation_version (`str`): Row derivation version.

        Returns:
            The decoded `ContextSnapshot`.

        Raises:
            ValueError: If the payload is not valid JSON, carries an
                unknown schema version, or holds an invalid message.
        """

        document = _load_object(value)
        _require_version(
            document,
            CONTEXT_SNAPSHOT_SCHEMA_VERSION,
            label="context snapshot",
        )
        summary = document.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("context snapshot summary must be a string")
        entries = document.get("context")
        if not isinstance(entries, list):
            raise ValueError("context snapshot payload misses context")
        context = tuple(
            decode_journal_message(
                entry.get("role") if isinstance(entry, dict) else None,
                _dump(entry.get("payload")) if isinstance(entry, dict) else "",
            )
            for entry in entries
        )
        return cls(
            session_id=session_id,
            source_revision=source_revision,
            summary=summary,
            context=context,
            derivation_version=derivation_version,
        )


@dataclass(frozen=True, slots=True)
class ModelCallUsage:
    """One model call's accounting record.

    Records are deduplicated by ``model_call_id``, which the Runtime
    generates before issuing the provider request so crash retries never
    double-count tokens.

    Args:
        model_call_id (`str`): Runtime-generated globally unique call id.
        session_id (`str`): The session the call belongs to.
        purpose (`UsagePurpose`): Whether the call served the agent loop
            or context compaction.
        input_tokens (`int`): Input token count for the call.
        output_tokens (`int`): Output token count for the call.
        created_at (`datetime`): When the call completed.
    """

    model_call_id: str
    session_id: str
    purpose: UsagePurpose
    input_tokens: int
    output_tokens: int
    created_at: datetime


def serialize_system_prompt(system_message: SystemMessage) -> str:
    """Serialize a System Message to the audit-config form.

    System Messages never enter the journal; this form is captured once
    per session so the trace stays interpretable without the model's
    current catalog state.
    """

    return _dump({"blocks": [_text_block(block) for block in system_message.content]})


def encode_journal_message(message: ModelMessage) -> tuple[JournalRole, str]:
    """Serialize one journal message with the explicit payload schema version.

    Args:
        message (`ModelMessage`): The message to encode; a
            ``SystemMessage`` is not a valid journal entry.

    Returns:
        The ``(role, payload)`` row values for the journal.

    Raises:
        ValueError: If the message type cannot enter the journal.
    """

    if isinstance(message, UserMessage):
        return "user", _dump(
            {
                "schema_version": JOURNAL_PAYLOAD_SCHEMA_VERSION,
                "role": "user",
                "blocks": [_text_block(block) for block in message.content],
            }
        )
    if isinstance(message, AssistantMessage):
        return "assistant", _dump(
            {
                "schema_version": JOURNAL_PAYLOAD_SCHEMA_VERSION,
                "role": "assistant",
                "blocks": [
                    _tool_call_block(block)
                    if isinstance(block, ToolCall)
                    else _text_block(block)
                    for block in message.content
                ],
            }
        )
    if isinstance(message, ToolResultMessage):
        return "tool_result", _dump(
            {
                "schema_version": JOURNAL_PAYLOAD_SCHEMA_VERSION,
                "role": "tool_result",
                "results": [_tool_result(result) for result in message.content],
            }
        )
    raise ValueError(f"cannot serialize {type(message).__name__} to journal")


def decode_journal_message(role: object, payload: str) -> ModelMessage:
    """Deserialize one journal payload back into its provider-neutral message.

    Args:
        role (`object`): The role stored beside the payload; validated
            against the payload's own role envelope.
        payload (`str`): The persisted schema-versioned JSON payload.

    Returns:
        The decoded `ModelMessage`.

    Raises:
        ValueError: If the payload is not valid JSON, carries an unknown
            schema version, disagrees with ``role``, holds an unknown
            role, or has an invalid body shape.
    """

    document = _load_object(payload)
    _require_version(
        document,
        JOURNAL_PAYLOAD_SCHEMA_VERSION,
        label="journal payload",
    )
    if document.get("role") != role:
        raise ValueError(
            f"journal payload role {document.get('role')!r} "
            f"disagrees with row role {role!r}"
        )
    if role == "user":
        return UserMessage(
            content=tuple(
                TextBlock(text=block["text"])
                for block in _body_blocks(document, "user")
            )
        )
    if role == "assistant":
        blocks: list[TextBlock | ToolCall] = []
        for block in _body_blocks(document, "assistant"):
            if block.get("type") == "tool_call":
                blocks.append(
                    ToolCall(
                        call_id=block["call_id"],
                        name=block["name"],
                        arguments=block["arguments"],
                    )
                )
            else:
                blocks.append(TextBlock(text=block["text"]))
        return AssistantMessage(content=tuple(blocks))
    if role == "tool_result":
        results = document.get("results")
        if not isinstance(results, list):
            raise ValueError("tool_result payload misses results")
        return ToolResultMessage(
            content=tuple(
                ToolResult(
                    call_id=result["call_id"],
                    output=result.get("output"),
                    error=result.get("error"),
                )
                for result in results
            )
        )
    raise ValueError(f"unknown journal role: {role!r}")


def _body_blocks(document: dict[str, object], role: str) -> list[dict[str, object]]:
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError(f"{role} payload misses blocks")
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError(f"{role} payload holds a non-object block")
    return blocks


def _text_block(block: TextBlock) -> dict[str, str]:
    return {"type": "text", "text": block.text}


def _tool_call_block(block: ToolCall) -> dict[str, object]:
    return {
        "type": "tool_call",
        "call_id": block.call_id,
        "name": block.name,
        "arguments": block.arguments,
    }


def _tool_result(result: ToolResult) -> dict[str, object]:
    return {
        "call_id": result.call_id,
        "output": result.output,
        "error": result.error,
    }


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_object(value: str) -> dict[str, object]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("payload is not a JSON object")
    return document


def _require_version(
    document: dict[str, object],
    expected: int,
    *,
    label: str,
) -> None:
    version = document.get("schema_version")
    if version != expected:
        raise ValueError(
            f"unknown {label} schema version: {version!r} (expected {expected!r})"
        )
