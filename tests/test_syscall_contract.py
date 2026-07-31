import json
from dataclasses import FrozenInstanceError
from inspect import signature

import pytest

from cli_agent.runtime import ModelRequest, UserMessage
from cli_agent.runtime._syscalls import BUILT_IN_SYSCALL_SCHEMAS


def test_model_request_exposes_the_exact_fixed_builtin_tools() -> None:
    request = ModelRequest(messages=(UserMessage.text("inspect the workspace"),))

    assert request.tools is BUILT_IN_SYSCALL_SCHEMAS
    assert len(request.tools) == 3
    assert tuple(schema.name for schema in request.tools) == (
        "exec",
        "output",
        "kill",
    )
    assert [schema.to_json() for schema in request.tools] == _expected_schemas()


def test_builtin_tool_shape_is_json_serializable_and_immutable() -> None:
    request = ModelRequest(messages=())
    serialized = json.dumps([schema.to_json() for schema in request.tools])

    assert json.loads(serialized) == _expected_schemas()

    with pytest.raises(FrozenInstanceError):
        request.tools[0].name = "dynamic_exec"  # type: ignore[misc]


def test_runtime_capability_metadata_cannot_change_builtin_tools() -> None:
    capability_metadata = {
        "skills": ["deploy"],
        "tools": ["search"],
        "library": ["runbook"],
        "mcp": ["tickets.create"],
    }
    first_request = ModelRequest(messages=())

    capability_metadata["tools"].append("workspace.write")
    capability_metadata["mcp"].clear()
    second_request = ModelRequest(messages=())

    assert first_request.tools is second_request.tools
    assert tuple(schema.name for schema in second_request.tools) == (
        "exec",
        "output",
        "kill",
    )
    assert not set(capability_metadata["skills"] + capability_metadata["tools"]) & {
        schema.name for schema in second_request.tools
    }


def test_callers_cannot_supply_different_tools_to_model_request() -> None:
    assert tuple(signature(ModelRequest).parameters) == ("messages",)

    with pytest.raises(TypeError):
        ModelRequest(messages=(), tools=())  # type: ignore[call-arg]


def _expected_schemas() -> list[dict[str, object]]:
    output_schema = {
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
    return [
        {
            "name": "exec",
            "description": (
                "Start one command in the current bound environment session and return "
                "its current execution snapshot."
            ),
            "input_schema": {
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
                        "description": (
                            "Maximum number of retained output chunks to return."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            "output_schema": output_schema,
        },
        {
            "name": "output",
            "description": (
                "Read retained incremental output for an execution in the current bound "
                "environment session without consuming it."
            ),
            "input_schema": {
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
                        "description": (
                            "Maximum number of retained output chunks to return."
                        ),
                    },
                    "wait_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": (
                            "Maximum time to wait for output or a terminal state."
                        ),
                    },
                },
                "required": ["exec_id"],
                "additionalProperties": False,
            },
            "output_schema": output_schema,
        },
        {
            "name": "kill",
            "description": (
                "Terminate a queued or running execution in the current bound environment "
                "session and return its execution snapshot."
            ),
            "input_schema": {
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
                        "description": (
                            "Maximum number of retained output chunks to return."
                        ),
                    },
                },
                "required": ["exec_id"],
                "additionalProperties": False,
            },
            "output_schema": output_schema,
        },
    ]
