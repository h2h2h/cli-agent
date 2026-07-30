"""Host-facing Runtime lifecycle."""

from __future__ import annotations

import keyword
from collections.abc import AsyncIterator, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from cli_agent.runtime._agent_loop import AgentLoop
from cli_agent.runtime._capability_view import _CapabilityView
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.policy import (
    _DEFAULT_APPROVAL_CAPACITY,
    _DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ExecutablePolicy,
    ExecutionApprover,
    ExecutionPolicy,
    _ExecutionApprovalGate,
    _validate_approval_capacity,
    _validate_approval_timeout,
)
from cli_agent.runtime._environment.scheduler import (
    _DEFAULT_PARALLEL_LIMIT,
    _DEFAULT_QUEUE_LIMIT,
    _validate_parallel_limit,
    _validate_queue_limit,
)
from cli_agent.runtime._system_message import assemble_system_message
from cli_agent.runtime._tool_catalog import _ToolCatalog
from cli_agent.runtime._tool_environment import _ToolEnvironment
from cli_agent.runtime._workspace import (
    _load_workspace_environment,
    _prepare_workspace,
)
from cli_agent.runtime.model import ModelEvent, ModelProvider, UserMessage


class RuntimeClosedError(RuntimeError):
    """Raised when work is requested from a closed Agent Runtime."""


@dataclass(slots=True)
class _Session:
    kernel: EnvironmentKernel
    loop: AgentLoop


class AgentRuntime:
    """Own Workspace-scoped resources for the host."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        workspace: Path,
        capability_view: _CapabilityView,
        tool_catalog: _ToolCatalog,
        tool_environment: _ToolEnvironment,
        base_env: Mapping[str, str],
        policy: ExecutionPolicy,
        approval_gate: _ExecutionApprovalGate | None,
        queue_limit: int,
        parallel_limit: int,
        tool_parallel_limit: int,
        parallel_commands: frozenset[str],
        parallel_tools: frozenset[str],
        instruction: str | None,
    ) -> None:
        self._provider = provider
        self._workspace = workspace
        self._capability_view = capability_view
        self._tool_catalog = tool_catalog
        self._tool_environment = tool_environment
        self._base_env = base_env
        self._policy = policy
        self._approval_gate = approval_gate
        self._queue_limit = queue_limit
        self._parallel_limit = parallel_limit
        self._tool_parallel_limit = tool_parallel_limit
        self._parallel_commands = parallel_commands
        self._parallel_tools = parallel_tools
        self._instruction = instruction
        self._sessions: dict[str, _Session] = {}
        self._closed = False

    @classmethod
    def open(
        cls,
        *,
        workspace: str | Path,
        repertoire: str | Path | None = None,
        provider: ModelProvider,
        system_instruction: str | None = None,
        execution_policy: ExecutionPolicy | None = None,
        execution_approver: ExecutionApprover | None = None,
        pending_approval_capacity: int = _DEFAULT_APPROVAL_CAPACITY,
        approval_timeout_seconds: float = _DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        pending_execution_capacity: int = _DEFAULT_QUEUE_LIMIT,
        parallel_execution_capacity: int = _DEFAULT_PARALLEL_LIMIT,
        parallel_tool_execution_capacity: int = _DEFAULT_PARALLEL_LIMIT,
        parallel_shell_commands: frozenset[str] | None = None,
        parallel_tools: frozenset[str] | None = None,
    ) -> _AgentRuntimeOpener:
        """Prepare to asynchronously open a Workspace-bound Runtime.

        ``repertoire`` selects the user-maintained capability lower and
        defaults to ``~/.config/cli-agent/repertoire``.
        ``system_instruction`` extends the canonical instruction assembled for
        each new Agent Session. ``execution_policy`` replaces the default
        executable Policy, which asks for recognized direct filesystem
        mutators and otherwise allows.
        ``execution_approver`` resolves ASK evaluations for this Runtime.
        Approval waits are bounded by ``pending_approval_capacity`` and
        ``approval_timeout_seconds`` without consuming Execution capacity.
        ``pending_execution_capacity`` bounds queued Executions in each Session
        and defaults to 32. ``parallel_execution_capacity`` bounds a batch of
        trusted parallel-safe commands; ``parallel_shell_commands`` grants that
        scheduling class to simple direct invocations of those executables.
        ``parallel_tool_execution_capacity`` independently bounds the Tool
        lane; only names in the Host-owned ``parallel_tools`` set may run
        concurrently within that lane.
        """

        return _AgentRuntimeOpener(
            cls,
            workspace,
            repertoire,
            provider,
            system_instruction,
            execution_policy,
            execution_approver,
            _validate_approval_capacity(pending_approval_capacity),
            _validate_approval_timeout(approval_timeout_seconds),
            _validate_queue_limit(pending_execution_capacity),
            _validate_parallel_limit(parallel_execution_capacity),
            _validate_parallel_limit(parallel_tool_execution_capacity),
            frozenset(parallel_shell_commands or ()),
            _validate_parallel_tool_names(parallel_tools or frozenset()),
        )

    @classmethod
    async def _open(
        cls,
        workspace: str | Path,
        repertoire: str | Path | None,
        provider: ModelProvider,
        instruction: str | None,
        policy: ExecutionPolicy | None,
        approver: ExecutionApprover | None,
        approval_capacity: int,
        approval_timeout: float,
        queue_limit: int,
        parallel_limit: int,
        tool_parallel_limit: int,
        parallel_commands: frozenset[str],
        parallel_tools: frozenset[str],
    ) -> AgentRuntime:
        paths = _prepare_workspace(workspace)
        base_env = _load_workspace_environment(paths.environment)
        capability_view = _CapabilityView.open(paths.root, repertoire)
        tool_catalog = _ToolCatalog.reconcile(capability_view)
        tool_environment = await _ToolEnvironment.reconcile(capability_view)
        effective_policy = ExecutablePolicy() if policy is None else policy
        approval_gate = (
            None
            if approver is None
            else _ExecutionApprovalGate(
                approver,
                capacity=approval_capacity,
                timeout_seconds=approval_timeout,
            )
        )
        return cls(
            provider=provider,
            workspace=paths.root,
            capability_view=capability_view,
            tool_catalog=tool_catalog,
            tool_environment=tool_environment,
            base_env=base_env,
            policy=effective_policy,
            approval_gate=approval_gate,
            queue_limit=queue_limit,
            parallel_limit=parallel_limit,
            tool_parallel_limit=tool_parallel_limit,
            parallel_commands=parallel_commands,
            parallel_tools=parallel_tools,
            instruction=instruction,
        )

    @property
    def closed(self) -> bool:
        """Return whether the Runtime has been closed."""

        return self._closed

    async def close(self) -> None:
        """Close all Runtime-owned resources idempotently."""

        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await session.kernel.close()

    async def run_turn(
        self,
        session_id: str,
        message: UserMessage,
        *,
        provider: ModelProvider | None = None,
    ) -> AsyncIterator[ModelEvent]:
        """Run one turn in a get-or-created Agent Session.

        ``provider`` is used only when ``session_id`` is first seen.
        """

        self._ensure_open()
        session = self._sessions.get(session_id)
        if session is None:
            bound_provider = provider if provider is not None else self._provider
            system = assemble_system_message(
                self._workspace,
                self._instruction,
            )
            kernel = self._new_kernel(session_id)
            try:
                loop = AgentLoop(
                    bound_provider,
                    kernel,
                    system_message=system,
                )
            except BaseException:
                await kernel.close()
                raise
            session = _Session(
                kernel=kernel,
                loop=loop,
            )
            self._sessions[session_id] = session

        async for event in session.loop.run(message):
            yield event

    async def close_session(self, session_id: str) -> None:
        """Close and forget one Agent Session idempotently."""

        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.kernel.close()

    async def __aenter__(self) -> AgentRuntime:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError("AgentRuntime is closed")

    def _new_kernel(self, session_id: str) -> EnvironmentKernel:
        return EnvironmentKernel(
            self._workspace,
            capability_view=self._capability_view,
            tool_catalog=self._tool_catalog,
            tool_environment=self._tool_environment,
            base_env=self._base_env,
            policy=self._policy,
            approval_gate=self._approval_gate,
            approval_session_id=session_id,
            queue_limit=self._queue_limit,
            parallel_limit=self._parallel_limit,
            tool_parallel_limit=self._tool_parallel_limit,
            parallel_commands=self._parallel_commands,
            parallel_tools=self._parallel_tools,
        )


class _AgentRuntimeOpener:
    """Await or enter one Agent Runtime open operation."""

    def __init__(
        self,
        runtime_cls: type[AgentRuntime],
        workspace: str | Path,
        repertoire: str | Path | None,
        provider: ModelProvider,
        instruction: str | None,
        policy: ExecutionPolicy | None,
        approver: ExecutionApprover | None,
        approval_capacity: int,
        approval_timeout: float,
        queue_limit: int,
        parallel_limit: int,
        tool_parallel_limit: int,
        parallel_commands: frozenset[str],
        parallel_tools: frozenset[str],
    ) -> None:
        self._runtime_cls = runtime_cls
        self._workspace = workspace
        self._repertoire = repertoire
        self._provider = provider
        self._instruction = instruction
        self._policy = policy
        self._approver = approver
        self._approval_capacity = approval_capacity
        self._approval_timeout = approval_timeout
        self._queue_limit = queue_limit
        self._parallel_limit = parallel_limit
        self._tool_parallel_limit = tool_parallel_limit
        self._parallel_commands = parallel_commands
        self._parallel_tools = parallel_tools
        self._runtime: AgentRuntime | None = None

    def __await__(self) -> Generator[Any, None, AgentRuntime]:
        return self._open().__await__()

    async def __aenter__(self) -> AgentRuntime:
        runtime = await self._open()
        return await runtime.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._runtime is not None:
            await self._runtime.__aexit__(
                exc_type,
                exc_value,
                traceback,
            )

    async def _open(self) -> AgentRuntime:
        if self._runtime is None:
            self._runtime = await self._runtime_cls._open(
                self._workspace,
                self._repertoire,
                self._provider,
                self._instruction,
                self._policy,
                self._approver,
                self._approval_capacity,
                self._approval_timeout,
                self._queue_limit,
                self._parallel_limit,
                self._tool_parallel_limit,
                self._parallel_commands,
                self._parallel_tools,
            )
        else:
            self._runtime._ensure_open()
        return self._runtime


def _validate_parallel_tool_names(names: frozenset[str]) -> frozenset[str]:
    invalid = sorted(
        (
            name
            for name in names
            if (
                not isinstance(name, str)
                or not name.isidentifier()
                or keyword.iskeyword(name)
            )
        ),
        key=repr,
    )
    if invalid:
        raise ValueError(
            "parallel Tool names must be non-keyword Python identifiers"
        )
    return frozenset(names)
