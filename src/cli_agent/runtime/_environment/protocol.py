"""Model-visible AEP Syscall payload shapes and argument validation."""

from __future__ import annotations

import jsonschema

from cli_agent.runtime._environment.records import ExecutionRecord
from cli_agent.runtime._syscalls import BUILT_IN_SYSCALL_SCHEMAS
from cli_agent.runtime.model import JSONValue, ToolCall, ToolResult

_SCHEMA_BY_NAME = {
    schema.name: schema.input_schema for schema in BUILT_IN_SYSCALL_SCHEMAS
}


def _protocol_error(
    call_id: str,
    *,
    code: str,
    message: str,
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        error={"ok": False, "code": code, "message": message},
    )


def _validate_arguments(
    call: ToolCall,
    schema: dict[str, object],
) -> dict[str, JSONValue] | ToolResult:
    args = _apply_defaults(call.arguments, schema)
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as err:
        return _protocol_error(
            call.call_id,
            code="invalid_argument",
            message=err.message,
        )
    except jsonschema.SchemaError:
        return _protocol_error(
            call.call_id,
            code="internal",
            message="syscall schema is broken",
        )
    return args


def _apply_defaults(
    arguments: dict[str, JSONValue],
    schema: dict[str, object],
) -> dict[str, JSONValue]:
    """Apply default values to missing arguments in a tool call."""
    filled = dict(arguments)
    for key, prop in schema.get("properties", {}).items():
        if key not in filled and isinstance(prop, dict) and "default" in prop:
            filled[key] = prop["default"]
    return filled


def _snapshot(
    state: ExecutionRecord,
    *,
    cursor: int,
    limit: int,
) -> dict[str, JSONValue]:
    chunks = list(state.chunks[cursor : cursor + limit])
    next_cursor = cursor + len(chunks)
    return {
        "ok": True,
        "exec_id": state.exec_id,
        "status": state.status,
        "exit_code": state.exit_code,
        "chunks": chunks,
        "next_cursor": next_cursor,
        "is_terminal": state.is_terminal,
        "truncated": state.truncated,
        "available_from": 0,
    }
