"""Environment Kernel with one Shell execution tracer bullet."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from cli_agent.runtime._environment.policy import (
    CommandParseResult,
    DirectExecutableDenyPolicy,
    ExecutionDecision,
    ExecutionPolicy,
    parse_shell_command,
)
from cli_agent.runtime.model import JSONValue, ToolCall, ToolResult

_BUILTIN_TOOL_NAMES = frozenset({"exec", "output", "kill"})
_TERMINAL_STATUSES = frozenset({"exited", "failed", "killed"})
_OUTPUT_CHUNK_SIZE = 4096
_DEFAULT_OUTPUT_CHUNK_BOUND = 2_000
_DEFAULT_OUTPUT_BYTE_BOUND = 1_048_576
_DEFAULT_PENDING_EXECUTION_CAPACITY = 32
_TERMINATE_GRACE_SECONDS = 0.5


class _ExecutionLane(Enum):
    SHELL = "shell"


@dataclass(slots=True)
class _ExecutionRecord:
    exec_id: str
    decision: ExecutionDecision
    lane: _ExecutionLane
    status: str = "queued"
    submission_sequence: int | None = None
    exit_code: int | None = None
    chunks: list[dict[str, JSONValue]] = field(default_factory=list)
    retained_bytes: int = 0
    truncated: bool = False
    kill_requested: bool = False
    process: asyncio.subprocess.Process | None = None
    completion_task: asyncio.Task[None] | None = None
    process_ready: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class _SchedulerAdmission:
    record: _ExecutionRecord
    runnable: tuple[_ExecutionRecord, ...]


class _ExecutionScheduler:
    """Assign admitted Executions to Runtime-trusted Driver lanes."""

    def __init__(self, pending_capacity: int) -> None:
        self._pending_capacity = _validate_pending_execution_capacity(pending_capacity)
        self._next_submission_sequence = 0
        self._pending: list[_ExecutionRecord] = []
        self._running: dict[_ExecutionLane, set[str]] = {
            _ExecutionLane.SHELL: set(),
        }
        self._lane_capacities = {
            _ExecutionLane.SHELL: 1,
        }
        self._closed = False

    def admit(
        self,
        decision: ExecutionDecision,
        lane: _ExecutionLane,
    ) -> _SchedulerAdmission | None:
        """Accept allowed work when its required pending capacity is available."""

        if self._closed:
            raise RuntimeError("Execution Scheduler is closed")
        if not decision.allowed:
            raise RuntimeError("Execution Scheduler received a denied decision")
        if (
            len(self._running[lane]) >= self._lane_capacities[lane]
            and len(self._pending) >= self._pending_capacity
        ):
            return None

        record = _ExecutionRecord(
            exec_id=uuid4().hex,
            decision=decision,
            lane=lane,
            submission_sequence=self._next_submission_sequence,
        )
        self._next_submission_sequence += 1
        self._pending.append(record)
        return _SchedulerAdmission(
            record=record,
            runnable=self._claim_runnable(),
        )

    def complete(self, record: _ExecutionRecord) -> tuple[_ExecutionRecord, ...]:
        """Release one running lane occupant and select follow-up work."""

        self._running[record.lane].discard(record.exec_id)
        if self._closed:
            return ()
        return self._claim_runnable()

    def cancel_pending(self, record: _ExecutionRecord) -> bool:
        """Atomically terminate one queued Execution before lane claim."""

        for index, pending in enumerate(self._pending):
            if pending is record:
                self._pending.pop(index)
                record.kill_requested = True
                record.status = "killed"
                return True
        return False

    def close(self) -> tuple[_ExecutionRecord, ...]:
        """Stop promotion and release all queued Executions."""

        if self._closed:
            return ()
        self._closed = True
        pending = tuple(self._pending)
        self._pending.clear()
        return pending

    def _claim_runnable(self) -> tuple[_ExecutionRecord, ...]:
        claimed: list[_ExecutionRecord] = []
        index = 0
        while index < len(self._pending):
            record = self._pending[index]
            running = self._running[record.lane]
            if len(running) >= self._lane_capacities[record.lane]:
                index += 1
                continue

            self._pending.pop(index)
            running.add(record.exec_id)
            record.status = "running"
            claimed.append(record)
        return tuple(claimed)


@dataclass(slots=True)
class _EnvironmentSession:
    scheduler: _ExecutionScheduler
    executions: dict[str, _ExecutionRecord] = field(default_factory=dict)
    closing: bool = False


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
        self._output_chunk_bound = output_chunk_bound
        self._output_byte_bound = output_byte_bound
        self._pending_execution_capacity = validated_pending_capacity
        self._sessions: dict[str, _EnvironmentSession] = {}
        self._closed = False

    def create_binding(self) -> EnvironmentBinding:
        """Create one private Environment Session and return its binding."""

        if self._closed:
            raise RuntimeError("EnvironmentKernel is closed")

        session_id = uuid4().hex
        self._sessions[session_id] = _EnvironmentSession(
            scheduler=_ExecutionScheduler(self._pending_execution_capacity)
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
            (session, self._begin_session_close(session)) for session in sessions
        )
        for session, pending in pending_by_session:
            await self._release_session(session, pending)

    async def _close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            pending = self._begin_session_close(session)
            await self._release_session(session, pending)

    def _begin_session_close(
        self,
        session: _EnvironmentSession,
    ) -> tuple[_ExecutionRecord, ...]:
        if session.closing:
            return ()
        session.closing = True
        pending = session.scheduler.close()
        for record in pending:
            record.kill_requested = True
            record.status = "killed"
            record.process_ready.set()
        return pending

    async def _release_session(
        self,
        session: _EnvironmentSession,
        pending: tuple[_ExecutionRecord, ...],
    ) -> None:
        for record in pending:
            await _notify_changed(record)
        for record in tuple(session.executions.values()):
            await self._terminate_execution(session, record)
        session.executions.clear()

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
        _reject_unknown_arguments(
            call.arguments,
            allowed={"command", "wait_ms", "output_limit"},
        )
        command = parse_shell_command(
            raw_command=_required_string(call.arguments, "command"),
            cwd=self._workspace,
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
            lane = _lane_for_decision(decision)
        except RuntimeError:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution route is not supported",
            )

        admission = session.scheduler.admit(
            decision,
            lane,
        )
        if admission is None:
            return _protocol_error(
                call.call_id,
                code="queue_full",
                message="execution pending queue is full",
            )

        record = admission.record
        session.executions[record.exec_id] = record
        for runnable in admission.runnable:
            self._start_execution(session, runnable)

        if command.wait_ms > 0 and not record.is_terminal:
            with suppress(asyncio.TimeoutError):
                async with record.changed:
                    await asyncio.wait_for(
                        record.changed.wait_for(lambda: record.is_terminal),
                        timeout=command.wait_ms / 1000,
                    )

        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(record, cursor=0, limit=command.output_limit),
        )

    def _start_execution(
        self,
        session: _EnvironmentSession,
        record: _ExecutionRecord,
    ) -> None:
        if record.lane is not _ExecutionLane.SHELL:
            raise RuntimeError("unsupported Execution lane")
        record.completion_task = asyncio.create_task(self._run_shell(session, record))

    async def _run_shell(
        self,
        session: _EnvironmentSession,
        record: _ExecutionRecord,
    ) -> None:
        command = _allowed_command(record.decision)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_shell(
                command.raw_command,
                cwd=command.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            record.process = process
            record.process_ready.set()
            if record.kill_requested:
                _signal_process(process, force=False)

            stdout_task = asyncio.create_task(
                self._capture_stream(record, process.stdout, "stdout")
            )
            stderr_task = asyncio.create_task(
                self._capture_stream(record, process.stderr, "stderr")
            )
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            record.exit_code = exit_code
            record.status = (
                "killed"
                if record.kill_requested
                else "exited"
                if exit_code == 0
                else "failed"
            )
        except Exception:
            if process is not None:
                _signal_process(process, force=True)
                with suppress(Exception):
                    await process.wait()
            record.status = "killed" if record.kill_requested else "failed"
            record.exit_code = process.returncode if process is not None else None
        finally:
            record.process_ready.set()
            for runnable in session.scheduler.complete(record):
                self._start_execution(session, runnable)
            await _notify_changed(record)

    async def _capture_stream(
        self,
        record: _ExecutionRecord,
        stream: asyncio.StreamReader | None,
        stream_name: str,
    ) -> None:
        if stream is None:
            return

        while data := await stream.read(_OUTPUT_CHUNK_SIZE):
            if (
                len(record.chunks) >= self._output_chunk_bound
                or record.retained_bytes + len(data) > self._output_byte_bound
            ):
                record.truncated = True
                await _notify_changed(record)
                continue

            record.chunks.append(
                {
                    "cursor": len(record.chunks),
                    "stream": stream_name,
                    "text": data.decode("utf-8", errors="replace"),
                    "timestamp": _timestamp(),
                }
            )
            record.retained_bytes += len(data)
            await _notify_changed(record)

    async def _output(
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
        wait_ms = _optional_integer(
            call.arguments,
            "wait_ms",
            default=0,
            minimum=0,
        )

        record = session.executions.get(exec_id)
        if record is None:
            return _protocol_error(
                call.call_id,
                code="unknown_execution",
                message="execution not found",
            )
        if wait_ms > 0 and cursor >= len(record.chunks) and not record.is_terminal:
            with suppress(asyncio.TimeoutError):
                async with record.changed:
                    await asyncio.wait_for(
                        record.changed.wait_for(
                            lambda: cursor < len(record.chunks) or record.is_terminal
                        ),
                        timeout=wait_ms / 1000,
                    )
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(record, cursor=cursor, limit=limit),
        )

    async def _kill(
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
        await self._terminate_execution(session, record)
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(record, cursor=cursor, limit=limit),
        )

    async def _terminate_execution(
        self,
        session: _EnvironmentSession,
        record: _ExecutionRecord,
    ) -> None:
        async with record.termination_lock:
            if record.is_terminal:
                return

            if record.status == "queued" and session.scheduler.cancel_pending(record):
                record.process_ready.set()
                await _notify_changed(record)
                return

            record.kill_requested = True
            await record.process_ready.wait()
            process = record.process
            if process is not None and process.returncode is None:
                _signal_process(process, force=False)
                try:
                    await _wait_until_terminal(
                        record,
                        timeout=_TERMINATE_GRACE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    _signal_process(process, force=True)

            task = record.completion_task
            if task is not None:
                with suppress(Exception):
                    await task


class _InvalidArguments(ValueError):
    pass


def _allowed_command(decision: ExecutionDecision) -> CommandParseResult:
    if not decision.allowed:
        raise RuntimeError("execution plane received a denied decision")
    return decision.parse_result


def _lane_for_decision(decision: ExecutionDecision) -> _ExecutionLane:
    command = _allowed_command(decision)
    if command.operation == "shell.execute":
        return _ExecutionLane.SHELL
    raise RuntimeError(f"unsupported Execution operation: {command.operation}")


def _validate_pending_execution_capacity(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("pending_execution_capacity must be an integer >= 1")
    return value


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
        "is_terminal": record.is_terminal,
        "truncated": record.truncated,
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


def _signal_process(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        elif force:
            process.kill()
        else:
            process.terminate()


async def _notify_changed(record: _ExecutionRecord) -> None:
    async with record.changed:
        record.changed.notify_all()


async def _wait_until_terminal(
    record: _ExecutionRecord,
    *,
    timeout: float,
) -> None:
    async with record.changed:
        await asyncio.wait_for(
            record.changed.wait_for(lambda: record.is_terminal),
            timeout=timeout,
        )
