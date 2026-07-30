"""Environment Kernel control and execution pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import jsonschema

from cli_agent.runtime._builtin_tools import BUILDIN_TOOL_SCHEMA_DEFINITIONS
from cli_agent.runtime._capability_view import _CapabilityView
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
from cli_agent.runtime._environment.drivers.tool import _ToolDriver
from cli_agent.runtime._environment.execution import (
    _ExecutionState,
    _notify_changed,
    _snapshot,
    _StateOutput,
)
from cli_agent.runtime._environment.policy import (
    ApprovalResponse,
    ExecutablePolicy,
    ExecutionDecision,
    ExecutionPolicy,
    PolicyAction,
    PolicyEvaluation,
    _ApprovalResolutionError,
    _ExecutionApprovalGate,
)
from cli_agent.runtime._environment.routing import (
    _CommandRouter,
    _DriverKind,
    _ExecutionRoute,
    _route_decision,
    _SchedulingClass,
)
from cli_agent.runtime._environment.scheduler import (
    _DEFAULT_PARALLEL_LIMIT,
    _DEFAULT_QUEUE_LIMIT,
    _ExecutionScheduler,
)
from cli_agent.runtime._tool_catalog import _ToolCatalog
from cli_agent.runtime._tool_commands import _ToolCommandClassifier
from cli_agent.runtime._tool_environment import _ToolEnvironment
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
        tool_parallel_limit: int = _DEFAULT_PARALLEL_LIMIT,
        parallel_commands: frozenset[str] | None = None,
        parallel_tools: frozenset[str] | None = None,
        registry: _CustomCommandRegistry | None = None,
        approval_gate: _ExecutionApprovalGate | None = None,
        approval_session_id: str | None = None,
        capability_view: _CapabilityView | None = None,
        tool_catalog: _ToolCatalog | None = None,
        tool_environment: _ToolEnvironment | None = None,
    ) -> None:
        root = Path(workspace).resolve()
        if not root.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        if chunk_limit < 1:
            raise ValueError("chunk_limit must be >= 1")
        if byte_limit < 1:
            raise ValueError("byte_limit must be >= 1")

        self._workspace = root
        self._policy = ExecutablePolicy() if policy is None else policy
        self._approval_gate = approval_gate
        self._approval_session_id = approval_session_id
        self._chunk_limit = chunk_limit
        self._byte_limit = byte_limit
        registry = (
            _CustomCommandRegistry(_builtin_custom_commands())
            if registry is None
            else registry
        )
        self._router = _CommandRouter(
            shell_driver=_ShellDriver(capability_view),
            custom_driver=_CustomDriver(registry),
            tool_driver=(
                _ToolDriver(tool_catalog, tool_environment)
                if tool_catalog is not None and tool_environment is not None
                else None
            ),
            parallel_commands=frozenset(parallel_commands or ()),
            parallel_tools=frozenset(parallel_tools or ()),
        )
        self._parser = ShlexCommandParser()
        self._tool_classifier = (
            _ToolCommandClassifier(tool_catalog)
            if tool_catalog is not None
            else None
        )
        self._scheduler = _ExecutionScheduler(
            queue_limit,
            parallel_limit,
            tool_parallel_limit,
        )
        self._env = dict(base_env or {})
        self._cwd = self._workspace
        self._executions: dict[str, _ExecutionState] = {}
        self._approval_tasks: set[asyncio.Task[object]] = set()
        self._closed = False

    async def close(self) -> None:
        """Close this Session-scoped Kernel idempotently."""

        if self._closed:
            return
        self._closed = True
        approval_tasks = tuple(self._approval_tasks)
        for task in approval_tasks:
            task.cancel()
        if approval_tasks:
            await asyncio.gather(*approval_tasks, return_exceptions=True)
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

    async def dispatch_batch(
        self,
        calls: tuple[ToolCall, ...],
    ) -> tuple[ToolResult, ...]:
        """Admit model-returned calls in order, then await them concurrently."""

        admitted: list[tuple[ToolCall, ToolResult]] = []
        for call in calls:
            if call.name == "exec":
                result = await self._exec(call, wait_for_completion=False)
            else:
                result = await self.dispatch(call)
            admitted.append((call, result))

        return tuple(
            await asyncio.gather(
                *(
                    self._await_initial_exec(call, result)
                    for call, result in admitted
                )
            )
        )

    async def _exec(
        self,
        call: ToolCall,
        *,
        wait_for_completion: bool = True,
    ) -> ToolResult:
        args = _validate_arguments(call, _SCHEMA_BY_NAME["exec"])
        if isinstance(args, ToolResult):
            return args
        command = self._parser.parse(args["command"])
        if self._tool_classifier is not None:
            command = self._tool_classifier.classify(command)
        try:
            evaluation = await self._policy.evaluate(command)
        except Exception:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution policy failed closed",
            )
        if (
            not isinstance(evaluation, PolicyEvaluation)
            or evaluation.parse_result != command
        ):
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution policy failed closed",
            )
        authorization = await self._authorize(
            call.call_id,
            evaluation,
        )
        if isinstance(authorization, ToolResult):
            return authorization
        decision = authorization
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

        if wait_for_completion and args["wait_ms"] > 0 and not state.is_terminal:
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

    async def _await_initial_exec(
        self,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        if call.name != "exec" or result.error is not None:
            return result
        args = _validate_arguments(call, _SCHEMA_BY_NAME["exec"])
        if isinstance(args, ToolResult):
            return result
        output = result.output
        if not isinstance(output, dict):
            return result
        exec_id = output.get("exec_id")
        if not isinstance(exec_id, str):
            return result
        state = self._executions.get(exec_id)
        if state is None:
            return result
        if args["wait_ms"] > 0 and not state.is_terminal:
            with suppress(asyncio.TimeoutError):
                async with state.changed:
                    await asyncio.wait_for(
                        state.changed.wait_for(lambda: state.is_terminal),
                        timeout=args["wait_ms"] / 1000,
                    )
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(
                state,
                cursor=0,
                limit=args["output_limit"],
            ),
        )

    async def _authorize(
        self,
        call_id: str,
        evaluation: PolicyEvaluation,
    ) -> ExecutionDecision | ToolResult:
        if evaluation.action is PolicyAction.DENY:
            return _protocol_error(
                call_id,
                code="policy_denied",
                message=evaluation.reason or "execution denied by policy",
            )
        if evaluation.action is PolicyAction.ALLOW:
            return ExecutionDecision.allow(
                evaluation.parse_result,
                rule_id=evaluation.rule_id,
            )

        gate = self._approval_gate
        if gate is None:
            return _protocol_error(
                call_id,
                code="policy_denied",
                message="execution requires approval but no approver is configured",
            )

        task = asyncio.create_task(
            gate.request(
                evaluation,
                session_id=self._approval_session_id,
            )
        )
        self._approval_tasks.add(task)
        try:
            try:
                resolution = await task
            except asyncio.CancelledError:
                if self._closed:
                    return _protocol_error(
                        call_id,
                        code="internal",
                        message="environment session is closed",
                    )
                raise
            except _ApprovalResolutionError as exc:
                return _protocol_error(
                    call_id,
                    code="policy_denied",
                    message=str(exc),
                )
        finally:
            self._approval_tasks.discard(task)

        if resolution.response is ApprovalResponse.DENY:
            return _protocol_error(
                call_id,
                code="policy_denied",
                message="execution approval was denied by the Host",
            )
        return ExecutionDecision.allow(
            evaluation.parse_result,
            rule_id=f"{evaluation.rule_id}.host-approved",
            approval_request_id=resolution.request_id,
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
        isolate_context = (
            state.route.scheduling is _SchedulingClass.PARALLEL_SAFE
            or state.route.driver_kind is _DriverKind.TOOL
        )
        context = _DriverContext(
            workspace=self._workspace,
            cwd=self._cwd,
            environment=dict(self._env) if isolate_context else self._env,
            set_cwd=None if isolate_context else self._set_cwd,
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
