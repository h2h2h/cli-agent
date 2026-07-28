"""Validation and result helpers for the fixed Environment tool protocol."""

from __future__ import annotations

from dataclasses import dataclass

from cli_agent.runtime.model import JSONValue, ToolCall, ToolResult

_BUILTIN_TOOL_NAMES = frozenset({"exec", "output", "kill"})


@dataclass(frozen=True, slots=True)
class _ExecRequest:
    command: str
    wait_ms: int
    output_limit: int


@dataclass(frozen=True, slots=True)
class _OutputRequest:
    exec_id: str
    cursor: int
    limit: int
    wait_ms: int


@dataclass(frozen=True, slots=True)
class _KillRequest:
    exec_id: str
    cursor: int
    limit: int


class _InvalidArguments(ValueError):
    pass


def _parse_exec_request(call: ToolCall) -> _ExecRequest:
    _reject_unknown_arguments(
        call.arguments,
        allowed={"command", "wait_ms", "output_limit"},
    )
    return _ExecRequest(
        command=_required_string(call.arguments, "command"),
        wait_ms=_optional_integer(
            call.arguments,
            "wait_ms",
            default=8000,
            minimum=0,
        ),
        output_limit=_optional_integer(
            call.arguments,
            "output_limit",
            default=200,
            minimum=1,
        ),
    )


def _parse_output_request(call: ToolCall) -> _OutputRequest:
    _reject_unknown_arguments(
        call.arguments,
        allowed={"exec_id", "cursor", "limit", "wait_ms"},
    )
    return _OutputRequest(
        exec_id=_required_string(call.arguments, "exec_id"),
        cursor=_optional_integer(
            call.arguments,
            "cursor",
            default=0,
            minimum=0,
        ),
        limit=_optional_integer(
            call.arguments,
            "limit",
            default=200,
            minimum=1,
        ),
        wait_ms=_optional_integer(
            call.arguments,
            "wait_ms",
            default=0,
            minimum=0,
        ),
    )


def _parse_kill_request(call: ToolCall) -> _KillRequest:
    _reject_unknown_arguments(
        call.arguments,
        allowed={"exec_id", "cursor", "limit"},
    )
    return _KillRequest(
        exec_id=_required_string(call.arguments, "exec_id"),
        cursor=_optional_integer(
            call.arguments,
            "cursor",
            default=0,
            minimum=0,
        ),
        limit=_optional_integer(
            call.arguments,
            "limit",
            default=200,
            minimum=1,
        ),
    )


def _protocol_error(
    call_id: str,
    *,
    code: str,
    message: str,
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        error={
            "ok": False,
            "code": code,
            "message": message,
        },
    )


def _reject_unknown_arguments(
    arguments: dict[str, JSONValue],
    *,
    allowed: set[str],
) -> None:
    unknown = sorted(arguments.keys() - allowed)
    if unknown:
        raise _InvalidArguments(f"unexpected argument(s): {', '.join(unknown)}")


def _required_string(arguments: dict[str, JSONValue], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _InvalidArguments(f"{name} must be a non-empty string")
    return value


def _optional_integer(
    arguments: dict[str, JSONValue],
    name: str,
    *,
    default: int,
    minimum: int,
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _InvalidArguments(f"{name} must be an integer >= {minimum}")
    return value
