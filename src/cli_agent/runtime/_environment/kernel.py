"""Minimal Environment Kernel for completed short commands."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cli_agent.runtime.model import JSONValue, ToolCall, ToolResult

_BUILTIN_TOOL_NAMES = frozenset({"exec", "output", "kill"})


@dataclass(slots=True)
class _ExecutionRecord:
    exec_id: str
    status: str
    exit_code: int
    chunks: tuple[dict[str, JSONValue], ...]


@dataclass(slots=True)
class _EnvironmentSession:
    executions: dict[str, _ExecutionRecord] = field(default_factory=dict)


class EnvironmentBinding:
    """Route built-in tools to one hidden Environment Kernel session."""

    def __init__(self, kernel: EnvironmentKernel, session_id: str) -> None:
        self._kernel = kernel
        self._session_id = session_id
        self._closed = False

    async def dispatch(self, call: ToolCall) -> ToolResult:
        """Dispatch one provider-neutral Tool Call to the bound session."""

        if self._closed:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="environment session is closed",
            )
        return await self._kernel._dispatch(self._session_id, call)

    async def close(self) -> None:
        """Close the bound Environment Session idempotently."""

        if self._closed:
            return
        self._closed = True
        await self._kernel._close_session(self._session_id)


class EnvironmentKernel:
    """Own minimal Environment Session state for one Workspace."""

    def __init__(self, workspace: str | Path) -> None:
        workspace_path = Path(workspace).resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")

        self._workspace = workspace_path
        self._sessions: dict[str, _EnvironmentSession] = {}
        self._closed = False

    def create_binding(self) -> EnvironmentBinding:
        """Create one private Environment Session and return its binding."""

        if self._closed:
            raise RuntimeError("EnvironmentKernel is closed")

        session_id = uuid4().hex
        self._sessions[session_id] = _EnvironmentSession()
        return EnvironmentBinding(self, session_id)

    async def close(self) -> None:
        """Close all Environment Sessions and the Kernel idempotently."""

        if self._closed:
            return
        self._closed = True
        self._sessions.clear()

    async def _close_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def _dispatch(self, session_id: str, call: ToolCall) -> ToolResult:
        session = self._sessions.get(session_id)
        if self._closed or session is None:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="environment session is closed",
            )

        if call.name not in _BUILTIN_TOOL_NAMES:
            return _protocol_error(
                call.call_id,
                code="invalid_argument",
                message=f"unknown built-in tool: {call.name}",
            )

        try:
            if call.name == "exec":
                return await self._exec(session, call)
            if call.name == "output":
                return self._output(session, call)
            return self._kill(session, call)
        except _InvalidArguments as exc:
            return _protocol_error(
                call.call_id,
                code="invalid_argument",
                message=str(exc),
            )

    async def _exec(
        self,
        session: _EnvironmentSession,
        call: ToolCall,
    ) -> ToolResult:
        _reject_unknown_arguments(
            call.arguments,
            allowed={"command", "wait_ms", "output_limit"},
        )
        command = _required_string(call.arguments, "command")
        _optional_integer(call.arguments, "wait_ms", default=8000, minimum=0)
        output_limit = _optional_integer(
            call.arguments,
            "output_limit",
            default=200,
            minimum=1,
        )

        exec_id = uuid4().hex
        chunks: list[dict[str, JSONValue]] = []
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self._workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.gather(
                _capture_stream(process.stdout, "stdout", chunks),
                _capture_stream(process.stderr, "stderr", chunks),
            )
            exit_code = await process.wait()
        except Exception:
            await _terminate(process)
            return _protocol_error(
                call.call_id,
                code="internal",
                message="command execution failed",
            )

        record = _ExecutionRecord(
            exec_id=exec_id,
            status="exited" if exit_code == 0 else "failed",
            exit_code=exit_code,
            chunks=tuple(chunks),
        )
        session.executions[exec_id] = record
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(record, cursor=0, limit=output_limit),
        )

    def _output(
        self,
        session: _EnvironmentSession,
        call: ToolCall,
    ) -> ToolResult:
        _reject_unknown_arguments(
            call.arguments,
            allowed={"exec_id", "cursor", "limit", "wait_ms"},
        )
        exec_id = _required_string(call.arguments, "exec_id")
        cursor = _optional_integer(
            call.arguments,
            "cursor",
            default=0,
            minimum=0,
        )
        limit = _optional_integer(
            call.arguments,
            "limit",
            default=200,
            minimum=1,
        )
        _optional_integer(call.arguments, "wait_ms", default=0, minimum=0)

        record = session.executions.get(exec_id)
        if record is None:
            return _protocol_error(
                call.call_id,
                code="unknown_execution",
                message="execution not found",
            )
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(record, cursor=cursor, limit=limit),
        )

    def _kill(
        self,
        session: _EnvironmentSession,
        call: ToolCall,
    ) -> ToolResult:
        _reject_unknown_arguments(
            call.arguments,
            allowed={"exec_id", "cursor", "limit"},
        )
        exec_id = _required_string(call.arguments, "exec_id")
        cursor = _optional_integer(
            call.arguments,
            "cursor",
            default=0,
            minimum=0,
        )
        limit = _optional_integer(
            call.arguments,
            "limit",
            default=200,
            minimum=1,
        )

        record = session.executions.get(exec_id)
        if record is None:
            return _protocol_error(
                call.call_id,
                code="unknown_execution",
                message="execution not found",
            )
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(record, cursor=cursor, limit=limit),
        )


class _InvalidArguments(ValueError):
    pass


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


async def _capture_stream(
    stream: asyncio.StreamReader | None,
    stream_name: str,
    chunks: list[dict[str, JSONValue]],
) -> None:
    if stream is None:
        return

    while data := await stream.read(4096):
        chunks.append(
            {
                "cursor": len(chunks),
                "stream": stream_name,
                "text": data.decode("utf-8", errors="replace"),
                "timestamp": _timestamp(),
            }
        )


def _snapshot(
    record: _ExecutionRecord,
    *,
    cursor: int,
    limit: int,
) -> dict[str, JSONValue]:
    chunks = list(record.chunks[cursor : cursor + limit])
    next_cursor = cursor + len(chunks)
    return {
        "ok": True,
        "exec_id": record.exec_id,
        "status": record.status,
        "exit_code": record.exit_code,
        "chunks": chunks,
        "next_cursor": next_cursor,
        "is_terminal": True,
        "truncated": False,
        "available_from": 0,
    }


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


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _terminate(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.kill()
    await process.wait()
