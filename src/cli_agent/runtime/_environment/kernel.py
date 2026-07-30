"""Environment Kernel control and execution pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import jsonschema

from cli_agent.runtime._builtin_tools import BUILDIN_TOOL_SCHEMA_DEFINITIONS
from cli_agent.runtime._environment.command_parser import ShlexCommandParser
from cli_agent.runtime._environment.commands import (
    _builtin_custom_commands,
    _CustomCommandRegistry,
)
from cli_agent.runtime._environment.drivers.base import (
    _DriverContext,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.drivers.custom import _CustomDriver
from cli_agent.runtime._environment.drivers.executions import _InlineExecution
from cli_agent.runtime._environment.drivers.shell import _ShellDriver
from cli_agent.runtime._environment.execution import (
    _ExecutionState,
    _notify_changed,
    _snapshot,
    _StateOutput,
)
from cli_agent.runtime._environment.policy import (
    DirectExecutableDenyPolicy,
    ExecutionDecision,
    ExecutionPolicy,
)
from cli_agent.runtime._environment.routing import (
    _CommandRouter,
    _ExecutionRoute,
    _route_decision,
    _SchedulingClass,
)
from cli_agent.runtime._environment.scheduler import (
    _DEFAULT_PARALLEL_LIMIT,
    _DEFAULT_QUEUE_LIMIT,
    _ExecutionScheduler,
    _validate_parallel_limit,
    _validate_queue_limit,
)
from cli_agent.runtime.model import JSONValue, ToolCall, ToolResult

_DEFAULT_CHUNK_LIMIT = 2_000
_DEFAULT_BYTE_LIMIT = 1_048_576

_SCHEMA_BY_NAME = {
    schema.name: schema.input_schema for schema in BUILDIN_TOOL_SCHEMA_DEFINITIONS
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
            message="built-in tool schema is broken",
        )
    return args


class EnvironmentKernel:
    """Own one Agent Session's stateful Workspace execution environment."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        base_env: Mapping[str, str] | None = None,
        policy: ExecutionPolicy | None = None,
        chunk_limit: int = _DEFAULT_CHUNK_LIMIT,
        byte_limit: int = _DEFAULT_BYTE_LIMIT,
        queue_limit: int = _DEFAULT_QUEUE_LIMIT,
        parallel_limit: int = _DEFAULT_PARALLEL_LIMIT,
        parallel_commands: frozenset[str] | None = None,
        registry: _CustomCommandRegistry | None = None,
    ) -> None:
        root = Path(workspace).resolve()
        if not root.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        if chunk_limit < 1:
            raise ValueError("chunk_limit must be >= 1")
        if byte_limit < 1:
            raise ValueError("byte_limit must be >= 1")

        self._workspace = root
        self._policy = (
            DirectExecutableDenyPolicy() if policy is None else policy
        )
        queue_limit = _validate_queue_limit(queue_limit)
        parallel_limit = _validate_parallel_limit(parallel_limit)
        self._chunk_limit = chunk_limit
        self._byte_limit = byte_limit
        registry = (
            _CustomCommandRegistry(_builtin_custom_commands())
            if registry is None
            else registry
        )
        self._router = _CommandRouter(
            shell_driver=_ShellDriver(),
            custom_driver=_CustomDriver(registry),
            parallel_shell_commands=frozenset(parallel_commands or ()),
        )
        self._parser = ShlexCommandParser()
        self._scheduler = _ExecutionScheduler(
            queue_limit,
            parallel_limit,
        )
        self._env = dict(base_env or {})
        self._cwd = self._workspace
        self._executions: dict[str, _ExecutionState] = {}
        self._closed = False

    async def close(self) -> None:
        """Close this Session-scoped Kernel idempotently."""

        if self._closed:
            return
        self._closed = True
        pending = self._scheduler.close()
        for state in pending:
            state.kill_requested = True
            state.status = "killed"
            await _notify_changed(state)
        for state in tuple(self._executions.values()):
            await self._terminate(state)
        self._executions.clear()
        self._env.clear()

    async def dispatch(self, call: ToolCall) -> ToolResult:
        """Dispatch one provider-neutral Tool Call in this Kernel."""

        if self._closed:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="environment session is closed",
            )

        if call.name not in _SCHEMA_BY_NAME:
            return _protocol_error(
                call.call_id,
                code="invalid_argument",
                message=f"unknown built-in tool: {call.name}",
            )

        if call.name == "exec":
            return await self._exec(call)
        if call.name == "output":
            return await self._output(call)
        return await self._kill(call)

    async def _exec(self, call: ToolCall) -> ToolResult:
        args = _validate_arguments(call, _SCHEMA_BY_NAME["exec"])
        if isinstance(args, ToolResult):
            return args
        command = self._parser.parse(args["command"])
        try:
            decision = await self._policy.decide(command)
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
        if self._closed:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="environment session is closed",
            )

        try:
            route = _route_decision(decision, self._router)
        except RuntimeError:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution route is not supported",
            )

        state = self._admit(decision, route)
        if state is None:
            return _protocol_error(
                call.call_id,
                code="queue_full",
                message="execution pending queue is full",
            )

        if args["wait_ms"] > 0 and not state.is_terminal:
            with suppress(asyncio.TimeoutError):
                async with state.changed:
                    await asyncio.wait_for(
                        state.changed.wait_for(lambda: state.is_terminal),
                        timeout=args["wait_ms"] / 1000,
                    )

        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(state, cursor=0, limit=args["output_limit"]),
        )

    async def _output(self, call: ToolCall) -> ToolResult:
        args = _validate_arguments(call, _SCHEMA_BY_NAME["output"])
        if isinstance(args, ToolResult):
            return args
        state = self._executions.get(args["exec_id"])
        if state is None:
            return _protocol_error(
                call.call_id,
                code="unknown_execution",
                message="execution not found",
            )
        await self._wait_for_output(
            state,
            cursor=args["cursor"],
            wait_ms=args["wait_ms"],
        )
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(
                state,
                cursor=args["cursor"],
                limit=args["limit"],
            ),
        )

    async def _kill(self, call: ToolCall) -> ToolResult:
        args = _validate_arguments(call, _SCHEMA_BY_NAME["kill"])
        if isinstance(args, ToolResult):
            return args
        state = self._executions.get(args["exec_id"])
        if state is None:
            return _protocol_error(
                call.call_id,
                code="unknown_execution",
                message="execution not found",
            )
        await self._terminate(state)
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(
                state,
                cursor=args["cursor"],
                limit=args["limit"],
            ),
        )

    def _admit(
        self,
        decision: ExecutionDecision,
        route: _ExecutionRoute,
    ) -> _ExecutionState | None:
        admission = self._scheduler.admit(decision, route)
        if admission is None:
            return None

        state = admission.state
        self._executions[state.exec_id] = state
        for runnable in admission.runnable:
            self._start_execution(runnable)
        return state

    async def _wait_for_output(
        self,
        state: _ExecutionState,
        *,
        cursor: int,
        wait_ms: int,
    ) -> None:
        if wait_ms <= 0 or cursor < len(state.chunks) or state.is_terminal:
            return
        with suppress(asyncio.TimeoutError):
            async with state.changed:
                await asyncio.wait_for(
                    state.changed.wait_for(
                        lambda: cursor < len(state.chunks) or state.is_terminal
                    ),
                    timeout=wait_ms / 1000,
                )

    async def _terminate(self, state: _ExecutionState) -> None:
        async with state.termination_lock:
            if state.is_terminal:
                return

            if state.status == "queued" and self._scheduler.cancel_pending(state):
                await _notify_changed(state)
                return

            state.kill_requested = True
            execution = state.driver_execution
            if execution is None:
                raise RuntimeError("running Execution has no Driver Execution")
            await execution.cancel()
            if state.completion_task is not None:
                with suppress(Exception):
                    await state.completion_task

    def _start_execution(self, state: _ExecutionState) -> None:
        is_parallel = state.route.scheduling is _SchedulingClass.PARALLEL_SAFE
        context = _DriverContext(
            workspace=self._workspace,
            cwd=self._cwd,
            environment=dict(self._env) if is_parallel else self._env,
            set_cwd=None if is_parallel else self._set_cwd,
        )
        try:
            execution = state.route.driver.prepare(
                state.decision.parse_result,
                context,
            )
        except Exception:
            execution = _InlineExecution(_preparation_failed)
        state.driver_execution = execution
        state.completion_task = asyncio.create_task(self._run_execution(state))

    def _set_cwd(self, cwd: Path) -> None:
        self._cwd = cwd

    async def _run_execution(self, state: _ExecutionState) -> None:
        execution = state.driver_execution
        if execution is None:
            raise RuntimeError("running Execution has no Driver Execution")
        output = _StateOutput(
            state,
            chunk_bound=self._chunk_limit,
            byte_bound=self._byte_limit,
        )
        try:
            outcome = await execution.run(output)
        except Exception:
            outcome = (
                _ExecutionOutcome.killed()
                if state.kill_requested
                else _ExecutionOutcome.failed()
            )
        state.status = outcome.status
        state.exit_code = outcome.exit_code
        for runnable in self._scheduler.complete(state):
            self._start_execution(runnable)
        await _notify_changed(state)


async def _preparation_failed(output: _ExecutionOutput) -> _ExecutionOutcome:
    del output
    return _ExecutionOutcome.failed()
