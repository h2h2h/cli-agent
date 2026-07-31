"""Environment Kernel: Session state aggregate and execution control plane."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from cli_agent.runtime._capability.command_parser import parse_shell_command
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.environment import _ToolEnvironment
from cli_agent.runtime._capability.tools.grammar import classify_tool_command
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._environment.commands import (
    _builtin_custom_commands,
    _CustomCommandRegistry,
)
from cli_agent.runtime._environment.drivers.shell import _ShellDriver
from cli_agent.runtime._environment.drivers.tool import _ToolDriver
from cli_agent.runtime._environment.execution import _ExecutionState
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
from cli_agent.runtime._environment.protocol import (
    _SCHEMA_BY_NAME,
    _protocol_error,
    _snapshot,
    _validate_arguments,
)
from cli_agent.runtime._environment.routing import _CommandRouter
from cli_agent.runtime._environment.scheduler import (
    _DEFAULT_PARALLEL_LIMIT,
    _DEFAULT_QUEUE_LIMIT,
)
from cli_agent.runtime._environment.supervisor import _ExecutionSupervisor
from cli_agent.runtime.model import ToolCall, ToolResult


class EnvironmentKernel:
    """Own one Agent Session's stateful Workspace execution environment."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        base_env: Mapping[str, str] | None = None,
        policy: ExecutionPolicy | None = None,
        chunk_limit: int = 2_000,
        byte_limit: int = 1_048_576,
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
        self._workspace = Path(workspace).resolve()
        self._policy = ExecutablePolicy() if policy is None else policy
        self._approval_gate = approval_gate
        self._approval_session_id = approval_session_id
        registry = (
            _CustomCommandRegistry(_builtin_custom_commands())
            if registry is None
            else registry
        )
        self._router = _CommandRouter(
            shell_driver=_ShellDriver(capability_view),
            custom_registry=registry,
            tool_driver=(
                _ToolDriver(tool_catalog, tool_environment)
                if tool_catalog is not None and tool_environment is not None
                else None
            ),
            parallel_commands=frozenset(parallel_commands or ()),
            parallel_tools=frozenset(parallel_tools or ()),
        )
        self._tool_catalog = tool_catalog
        self._env = dict(base_env or {})
        self._cwd = self._workspace
        self._executions: dict[str, _ExecutionState] = {}
        self._approval_tasks: set[asyncio.Task[object]] = set()
        self._closed = False
        self._supervisor = _ExecutionSupervisor(
            self,
            queue_limit=queue_limit,
            parallel_limit=parallel_limit,
            tool_parallel_limit=tool_parallel_limit,
            chunk_limit=chunk_limit,
            byte_limit=byte_limit,
        )

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
        await self._supervisor.close()
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
                message=f"unknown syscall: {call.name}",
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
        command = parse_shell_command(args["command"])
        # command.tool (`CommandParseResult`) is always `None` when parsed from a raw command string.
        # _tool_catalog is used to judge if it is a tool command and enrich the `CommandParseResult`.
        if self._tool_catalog is not None:
            command = classify_tool_command(command, self._tool_catalog)
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
        # evaluation is one of `allow`, `deny`, or `ask`
        # if `ask`, the approver_gate will be used in _authroze 
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
            route = self._router.route(decision)
        except RuntimeError:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution route is not supported",
            )

        state = self._supervisor.admit(decision, route)
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
        await self._supervisor.wait_for_output(
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
        await self._supervisor.terminate(state)
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(
                state,
                cursor=args["cursor"],
                limit=args["limit"],
            ),
        )

    def _set_cwd(self, cwd: Path) -> None:
        self._cwd = cwd
