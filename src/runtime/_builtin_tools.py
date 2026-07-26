"""Fixed provider-neutral built-in tool contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """A provider-neutral definition of one built-in tool."""

    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]

    def to_json(self) -> dict[str, object]:
        """Return the contract as a JSON-serializable object."""

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


_EXECUTION_OUTPUT_SCHEMA: dict[str, object] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "ok": {"const": True},
                "exec_id": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "enum": ["queued", "running", "exited", "failed", "killed"],
                },
                "exit_code": {
                    "oneOf": [{"type": "integer"}, {"type": "null"}],
                },
                "chunks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cursor": {"type": "integer", "minimum": 0},
                            "stream": {
                                "type": "string",
                                "enum": ["stdout", "stderr"],
                            },
                            "text": {"type": "string"},
                            "timestamp": {
                                "type": "string",
                                "format": "date-time",
                            },
                        },
                        "required": ["cursor", "stream", "text", "timestamp"],
                        "additionalProperties": False,
                    },
                },
                "next_cursor": {"type": "integer", "minimum": 0},
                "is_terminal": {"type": "boolean"},
                "truncated": {"type": "boolean"},
                "available_from": {"type": "integer", "minimum": 0},
            },
            "required": [
                "ok",
                "exec_id",
                "status",
                "exit_code",
                "chunks",
                "next_cursor",
                "is_terminal",
                "truncated",
                "available_from",
            ],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "ok": {"const": False},
                "code": {
                    "type": "string",
                    "enum": [
                        "invalid_argument",
                        "unknown_execution",
                        "queue_full",
                        "policy_denied",
                        "internal",
                    ],
                },
                "message": {"type": "string"},
            },
            "required": ["ok", "code", "message"],
            "additionalProperties": False,
        },
    ]
}

BUILDIN_TOOL_SCHEMA_DEFINITIONS = (
    ToolSchema(
        name="exec",
        description=(
            "Start one command in the current bound environment session and return "
            "its current execution snapshot."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The command to execute.",
                },
                "wait_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 8000,
                    "description": (
                        "Maximum time to wait for progress before returning; zero "
                        "submits without waiting."
                    ),
                },
                "output_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 200,
                    "description": "Maximum number of retained output chunks to return.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        output_schema=_EXECUTION_OUTPUT_SCHEMA,
    ),
    ToolSchema(
        name="output",
        description=(
            "Read retained incremental output for an execution in the current bound "
            "environment session without consuming it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "exec_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The execution ID returned by exec.",
                },
                "cursor": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "The output chunk cursor to continue from.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 200,
                    "description": "Maximum number of retained output chunks to return.",
                },
                "wait_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Maximum time to wait for output or a terminal state.",
                },
            },
            "required": ["exec_id"],
            "additionalProperties": False,
        },
        output_schema=_EXECUTION_OUTPUT_SCHEMA,
    ),
    ToolSchema(
        name="kill",
        description=(
            "Terminate a queued or running execution in the current bound environment "
            "session and return its execution snapshot."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "exec_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The execution ID returned by exec.",
                },
                "cursor": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": (
                        "The output chunk cursor to continue from after termination."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 200,
                    "description": "Maximum number of retained output chunks to return.",
                },
            },
            "required": ["exec_id"],
            "additionalProperties": False,
        },
        output_schema=_EXECUTION_OUTPUT_SCHEMA,
    ),
)
