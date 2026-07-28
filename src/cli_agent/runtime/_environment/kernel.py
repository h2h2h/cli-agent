"""Environment Kernel control and execution pipeline."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from cli_agent.runtime._environment.drivers.shell import _ShellDriver
from cli_agent.runtime._environment.execution import _snapshot
from cli_agent.runtime._environment.policy import (
    DirectExecutableDenyPolicy,
    ExecutionDecision,
    ExecutionPolicy,
    parse_shell_command,
)
from cli_agent.runtime._environment.protocol import (
    _BUILTIN_TOOL_NAMES,
    _InvalidArguments,
    _parse_exec_request,
    _parse_kill_request,
    _parse_output_request,
    _protocol_error,
)
from cli_agent.runtime._environment.routing import _route_decision
from cli_agent.runtime._environment.scheduler import (
    _DEFAULT_PENDING_EXECUTION_CAPACITY,
    _ExecutionScheduler,
    _validate_pending_execution_capacity,
)
from cli_agent.runtime._environment.supervisor import _EnvironmentSession
from cli_agent.runtime.model import ToolCall, ToolResult

_DEFAULT_OUTPUT_CHUNK_BOUND = 2_000
_DEFAULT_OUTPUT_BYTE_BOUND = 1_048_576


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
    """Own execution admission and lifecycle for one Workspace."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        execution_policy: ExecutionPolicy | None = None,
        output_chunk_bound: int = _DEFAULT_OUTPUT_CHUNK_BOUND,
        output_byte_bound: int = _DEFAULT_OUTPUT_BYTE_BOUND,
        pending_execution_capacity: int = _DEFAULT_PENDING_EXECUTION_CAPACITY,
    ) -> None:
        workspace_path = Path(workspace).resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        if output_chunk_bound < 1:
            raise ValueError("output_chunk_bound must be >= 1")
        if output_byte_bound < 1:
            raise ValueError("output_byte_bound must be >= 1")
        validated_pending_capacity = _validate_pending_execution_capacity(
            pending_execution_capacity
        )

        self._workspace = workspace_path
        self._execution_policy = (
            DirectExecutableDenyPolicy()
            if execution_policy is None
            else execution_policy
        )
        self._pending_execution_capacity = validated_pending_capacity
        self._shell_driver = _ShellDriver(
            output_chunk_bound=output_chunk_bound,
            output_byte_bound=output_byte_bound,
        )
        self._sessions: dict[str, _EnvironmentSession] = {}
        self._closed = False

    def create_binding(self) -> EnvironmentBinding:
        """Create one private Environment Session and return its binding."""

        if self._closed:
            raise RuntimeError("EnvironmentKernel is closed")

        session_id = uuid4().hex
        self._sessions[session_id] = _EnvironmentSession(
            scheduler=_ExecutionScheduler(self._pending_execution_capacity),
            shell_driver=self._shell_driver,
        )
        return EnvironmentBinding(self, session_id)

    async def close(self) -> None:
        """Close all Environment Sessions and the Kernel idempotently."""

        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        pending_by_session = tuple(
            (session, session.begin_close()) for session in sessions
        )
        for session, pending in pending_by_session:
            await session.release(pending)

    async def _close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            pending = session.begin_close()
            await session.release(pending)

    async def _dispatch(self, session_id: str, call: ToolCall) -> ToolResult:
        session = self._sessions.get(session_id)
        if self._closed or session is None or session.closing:
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
                return await self._output(session, call)
            return await self._kill(session, call)
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
        request = _parse_exec_request(call)
        command = parse_shell_command(
            raw_command=request.command,
            cwd=self._workspace,
            wait_ms=request.wait_ms,
            output_limit=request.output_limit,
        )
        try:
            decision = await self._execution_policy.decide(command)
        except Exception:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution policy failed closed",
            )
        if (
            not isinstance(decision, ExecutionDecision)
            or decision.parse_result != command
        ):
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution policy failed closed",
            )
        if not decision.allowed:
            return _protocol_error(
                call.call_id,
                code="policy_denied",
                message=decision.reason or "execution denied by policy",
            )
        if session.closing:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="environment session is closed",
            )

        try:
            route = _route_decision(decision)
        except RuntimeError:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution route is not supported",
            )

        record = session.admit(decision, route)
        if record is None:
            return _protocol_error(
                call.call_id,
                code="queue_full",
                message="execution pending queue is full",
            )

        if request.wait_ms > 0 and not record.is_terminal:
            with suppress(asyncio.TimeoutError):
                async with record.changed:
                    await asyncio.wait_for(
                        record.changed.wait_for(lambda: record.is_terminal),
                        timeout=request.wait_ms / 1000,
                    )

        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(record, cursor=0, limit=request.output_limit),
        )

    async def _output(
        self,
        session: _EnvironmentSession,
        call: ToolCall,
    ) -> ToolResult:
        request = _parse_output_request(call)
        record = session.executions.get(request.exec_id)
        if record is None:
            return _protocol_error(
                call.call_id,
                code="unknown_execution",
                message="execution not found",
            )
        await session.wait_for_output(
            record,
            cursor=request.cursor,
            wait_ms=request.wait_ms,
        )
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(
                record,
                cursor=request.cursor,
                limit=request.limit,
            ),
        )

    async def _kill(
        self,
        session: _EnvironmentSession,
        call: ToolCall,
    ) -> ToolResult:
        request = _parse_kill_request(call)
        record = session.executions.get(request.exec_id)
        if record is None:
            return _protocol_error(
                call.call_id,
                code="unknown_execution",
                message="execution not found",
            )
        await session.terminate(record)
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(
                record,
                cursor=request.cursor,
                limit=request.limit,
            ),
        )
