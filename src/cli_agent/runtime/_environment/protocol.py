"""Model-visible AEP Syscall payload shapes."""

from __future__ import annotations

from cli_agent.runtime._environment.records import ExecutionRecord
from cli_agent.runtime._syscalls import BUILT_IN_SYSCALL_SCHEMAS
from cli_agent.runtime.model import JSONValue, ToolResult

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
