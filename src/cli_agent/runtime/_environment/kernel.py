"""Environment Kernel: Session state aggregate and execution control plane."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from cli_agent.errors import error_boundary
from cli_agent.runtime._backend import _BackendWorkspace
from cli_agent.runtime._backend.local import _LocalBackendWorkspace
from cli_agent.runtime._capability.command_parser import (
    ShellParseResult,
    parse_shell_ast,
)
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._environment.handlers.base import _ExecutionRequest
from cli_agent.runtime._environment.interaction import (
    UserAnswer,
    UserInteraction,
    UserOption,
    UserQuestion,
)
from cli_agent.runtime._environment.manager import ExecutionManager
from cli_agent.runtime._environment.policy import (
    ExecutionPolicy,
    PolicyAction,
    PolicyEvaluation,
)
from cli_agent.runtime._environment.protocol import (
    _SCHEMA_BY_NAME,
    _protocol_error,
    _snapshot,
    _validate_arguments,
)
from cli_agent.runtime._environment.records import ExecutionRecord
from cli_agent.runtime._environment.routing import _CommandRouter
from cli_agent.runtime._environment.scheduler import (
    _DEFAULT_PARALLEL_LIMIT,
    _DEFAULT_QUEUE_LIMIT,
)
from cli_agent.runtime._environment.sources import (
    ExecutionSource,
    _builtin_inline_sources,
    _FileSource,
    _ShellSource,
    _SourceRegistry,
    _ToolSource,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import ToolCall, ToolResult

_ALLOW_ONCE_OPTIONS = (
    UserOption(value="allow_once", label="Allow once"),
    UserOption(value="deny", label="Deny"),
)


class EnvironmentKernel:
    """Own one Agent Session's stateful Workspace execution environment."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        backend: _BackendWorkspace | None = None,
        base_env: Mapping[str, str] | None = None,
        policy: ExecutionPolicy | None = None,
        chunk_limit: int = 2_000,
        byte_limit: int = 1_048_576,
        queue_limit: int = _DEFAULT_QUEUE_LIMIT,
        parallel_limit: int = _DEFAULT_PARALLEL_LIMIT,
        parallel_commands: frozenset[str] | None = None,
        custom_sources: Iterable[tuple[str, ExecutionSource]] = (),
        user_interaction: UserInteraction | None = None,
        session_id: str | None = None,
        library_catalog: _LibraryCatalog | None = None,
        tool_catalog: _ToolCatalog | None = None,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> None:
        host_workspace = Path(workspace).resolve()
        if backend is None:
            backend = _LocalBackendWorkspace(host_workspace, {})
        self._backend = backend
        self._workspace = backend.root
        self._policy = policy
        self._user_interaction = user_interaction
        self._session_id = session_id
        self._on_diagnostic = on_diagnostic
        self._library_catalog = library_catalog
        entries = list(_builtin_inline_sources(backend.filesystem))
        entries.append(
            (
                "files",
                _FileSource(
                    backend.filesystem,
                    mark_dirty=(
                        library_catalog.mark_path_dirty
                        if library_catalog is not None
                        else None
                    ),
                ),
            )
        )
        entries.append(("tools", _ToolSource(tool_catalog, backend)))
        entries.extend(custom_sources)
        self._router = _CommandRouter(
            shell_source=_ShellSource(
                backend,
                parallel_commands=frozenset(parallel_commands or ()),
            ),
            sources=_SourceRegistry(entries),
        )
        self._env = dict(base_env or {})
        self._cwd = backend.root
        self._executions: dict[str, ExecutionRecord] = {}
        self._interaction_tasks: set[asyncio.Task[object]] = set()
        self._closed = False
        self._manager = ExecutionManager(
            self,
            queue_limit=queue_limit,
            parallel_limit=parallel_limit,
            chunk_limit=chunk_limit,
            byte_limit=byte_limit,
        )

    async def close(self) -> None:
        """Close this Session-scoped Kernel idempotently."""

        if self._closed:
            return
        self._closed = True
        interaction_tasks = tuple(self._interaction_tasks)
        for task in interaction_tasks:
            task.cancel()
        if interaction_tasks:
            await asyncio.gather(*interaction_tasks, return_exceptions=True)
        await self._manager.close()
        self._env.clear()

    async def dispatch(self, call: ToolCall) -> ToolResult:
        """Dispatch one provider-neutral Tool Call in this Kernel."""

        with error_boundary(
            "kernel.dispatch",
            on_diagnostic=self._boundary_diagnostic,
        ):
            return await self._dispatch(call)

    async def _dispatch(self, call: ToolCall) -> ToolResult:
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

    async def reconcile_library(self) -> None:
        """Re-check Library source facts before one ordinary model request.

        Internal Library summary requests never pass through this hook; they
        call the provider directly from the Runtime-owned worker.
        """

        catalog = self._library_catalog
        if catalog is None:
            return
        try:
            await catalog.reconcile_changes()
        except Exception as exc:
            self._emit_diagnostic(
                "library.reconcile_failed",
                "library source reconcile failed",
                detail={"exception": repr(exc)},
            )

    async def dispatch_batch(
        self,
        calls: tuple[ToolCall, ...],
    ) -> tuple[ToolResult, ...]:
        """Admit model-returned calls in order, then await them concurrently."""

        with error_boundary(
            "kernel.dispatch",
            on_diagnostic=self._boundary_diagnostic,
        ):
            return await self._dispatch_batch(calls)

    async def _dispatch_batch(
        self,
        calls: tuple[ToolCall, ...],
    ) -> tuple[ToolResult, ...]:
        admitted: list[tuple[ToolCall, ToolResult]] = []
        for call in calls:
            if call.name == "exec":
                result = await self._exec(call, wait_for_completion=False)
            else:
                result = await self._dispatch(call)
            admitted.append((call, result))

        return tuple(
            await asyncio.gather(
                *(self._await_initial_exec(call, result) for call, result in admitted)
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
        command = parse_shell_ast(args["command"])
        if command.root is None:
            return _protocol_error(
                call.call_id,
                code="invalid_argument",
                message="invalid shell command",
            )
        try:
            route = self._router.resolve(command)
        except RuntimeError:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="execution route is not supported",
            )
        if self._policy is not None:
            evaluation = await self._evaluate(command, call.call_id)
            if isinstance(evaluation, ToolResult):
                return evaluation
            authorization = await self._authorize(
                call.call_id,
                evaluation,
                command,
            )
            if isinstance(authorization, ToolResult):
                return authorization
        if self._closed:
            return _protocol_error(
                call.call_id,
                code="internal",
                message="environment session is closed",
            )

        request = _ExecutionRequest(
            command=command,
            stdin=args.get("stdin"),
        )
        state = self._manager.admit(request, route)
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

    async def _evaluate(
        self,
        command: ShellParseResult,
        call_id: str,
    ) -> PolicyEvaluation | ToolResult:
        policy = self._policy
        if policy is None:
            raise RuntimeError("policy evaluation requires a configured policy")
        try:
            evaluation = await policy.evaluate(command)
        except Exception as exc:
            self._emit_diagnostic(
                "execution_policy.failed",
                "execution policy raised an exception",
                detail={"exception": repr(exc)},
            )
            return _protocol_error(
                call_id,
                code="policy_denied",
                message="execution policy failed closed",
            )
        if not _is_valid_evaluation(evaluation):
            self._emit_diagnostic(
                "execution_policy.invalid_evaluation",
                "execution policy returned an invalid evaluation",
                detail={"evaluation": repr(evaluation)},
            )
            return _protocol_error(
                call_id,
                code="policy_denied",
                message="execution policy failed closed",
            )
        return evaluation

    async def _authorize(
        self,
        call_id: str,
        evaluation: PolicyEvaluation,
        command: ShellParseResult,
    ) -> bool | ToolResult:
        if evaluation.action is PolicyAction.DENY:
            return _protocol_error(
                call_id,
                code="policy_denied",
                message=evaluation.reason or "execution denied by policy",
            )
        if evaluation.action is PolicyAction.ALLOW:
            return True

        interaction = self._user_interaction
        if interaction is None:
            return _protocol_error(
                call_id,
                code="policy_denied",
                message="execution requires user interaction but none is configured",
            )

        question = UserQuestion(
            request_id=uuid4().hex,
            session_id=self._session_id,
            prompt=_ask_prompt(evaluation, command),
            options=_ALLOW_ONCE_OPTIONS,
        )
        task = asyncio.create_task(interaction.ask(question))
        self._interaction_tasks.add(task)
        try:
            try:
                answer = await task
            except asyncio.CancelledError:
                if self._closed:
                    return _protocol_error(
                        call_id,
                        code="internal",
                        message="environment session is closed",
                    )
                raise
            except Exception as exc:
                self._emit_diagnostic(
                    "execution_interaction.failed",
                    "execution interaction raised an exception",
                    detail={"exception": repr(exc)},
                )
                return _protocol_error(
                    call_id,
                    code="policy_denied",
                    message="execution interaction failed closed",
                )
        finally:
            self._interaction_tasks.discard(task)

        if not _is_valid_answer(answer, question.options):
            self._emit_diagnostic(
                "execution_interaction.invalid_answer",
                "execution interaction returned an invalid answer",
                detail={"answer": repr(answer)},
            )
            return _protocol_error(
                call_id,
                code="policy_denied",
                message="execution interaction failed closed",
            )
        if answer.value == "allow_once":
            return True
        if answer.value == "deny":
            return _protocol_error(
                call_id,
                code="policy_denied",
                message=evaluation.reason or "execution denied by policy",
            )
        return _protocol_error(
            call_id,
            code="policy_denied",
            message="execution was not approved by the user",
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
        await self._manager.wait_for_output(
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
        await self._manager.terminate(state)
        return ToolResult(
            call_id=call.call_id,
            output=_snapshot(
                state,
                cursor=args["cursor"],
                limit=args["limit"],
            ),
        )

    def _set_cwd(self, cwd: str) -> None:
        self._cwd = cwd

    def _boundary_diagnostic(
        self,
        kind: str,
        message: str,
        detail: Mapping[str, object],
    ) -> None:
        """Sink used by ``error_boundary`` to emit boundary diagnostics."""

        self._emit_diagnostic(kind, message, detail=detail)

    def _emit_diagnostic(
        self,
        kind: str,
        message: str,
        *,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """Emit one structured Host notice when a callback is configured."""

        if self._on_diagnostic is None:
            return
        self._on_diagnostic(
            RuntimeDiagnostic(
                kind=kind,
                message=message,
                detail=detail or {},
            )
        )


def _is_valid_evaluation(evaluation: object) -> bool:
    """Return whether one Policy result is structurally valid."""

    return (
        isinstance(evaluation, PolicyEvaluation) and evaluation.action in PolicyAction
    )


def _ask_prompt(
    evaluation: PolicyEvaluation,
    command: ShellParseResult,
) -> str:
    """Build the standard ASK question prompt from a Policy conclusion."""

    reason = evaluation.reason or "execution requires Host approval"
    return f"{reason}\ncommand: {command.raw_command}"


def _is_valid_answer(answer: object, options: tuple[UserOption, ...]) -> bool:
    """Return whether one interaction answer is structurally valid."""

    if not isinstance(answer, UserAnswer):
        return False
    if answer.value is None:
        return True
    return answer.value in {option.value for option in options}
