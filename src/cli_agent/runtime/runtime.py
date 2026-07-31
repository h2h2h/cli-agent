"""Host-facing Runtime lifecycle."""

from __future__ import annotations

import keyword
from collections.abc import AsyncIterator, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from cli_agent.runtime._agent_loop import AgentLoop
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.policy import (
    ExecutablePolicy,
    ExecutionApprover,
    ExecutionPolicy,
    _ExecutionApprovalGate,
)
from cli_agent.runtime._system_message import assemble_system_message
from cli_agent.runtime.capability.tools.catalog import _ToolCatalog
from cli_agent.runtime.capability.tools.environment import _ToolEnvironment
from cli_agent.runtime.capability.view import _CapabilityView
from cli_agent.runtime.capability.workspace import (
    _load_workspace_env,
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
        provider: ModelProvider,
        repertoire: str | Path | None = None,
        system_instruction: str | None = None,
        execution_policy: ExecutionPolicy | None = None,
        execution_approver: ExecutionApprover | None = None,
        parallel_commands: frozenset[str] | None = None,
        parallel_tools: frozenset[str] | None = None,
    ) -> Coroutine[Any, None, AgentRuntime]:
        """Validate arguments and return a coroutine that opens the Runtime.

        ``parallel_tools`` is validated synchronously so non-identifier names
        raise before the caller awaits. Awaiting the returned coroutine
        reconciles the Workspace, Capability View, Tool Catalog, and Tool
        Environment, then constructs the Runtime. The opened Runtime may also
        be used as an async context manager that closes itself on exit.

        Args:
            workspace (`str | Path`):
                Existing directory to bind as the Workspace.
            provider (`ModelProvider`):
                Model provider used by new Sessions when no per-turn override
                is supplied to :meth:`run_turn`.
            repertoire (`str | Path | None`):
                User-maintained capability lower tree; defaults to
                ``~/.config/cli-agent/repertoire``.
            system_instruction (`str | None`):
                Optional Host instruction appended to the canonical per-Session
                system message.
            execution_policy (`ExecutionPolicy | None`):
                Replaces the default executable Policy.
            execution_approver (`ExecutionApprover | None`):
                Resolves ASK evaluations for this Runtime.
            parallel_commands (`frozenset[str] | None`):
                Executable basenames trusted to run in parallel Shell batches.
            parallel_tools (`frozenset[str] | None`):
                Tool names trusted to run in parallel Tool batches; each name
                must be a non-keyword Python identifier.

        Returns:
            A coroutine resolving to the opened :class:`AgentRuntime`.

        Raises:
            ValueError: If any name in ``parallel_tools`` is not a valid
                non-keyword Python identifier.
        """

        return cls._reconcile(
            workspace=workspace,
            repertoire=repertoire,
            provider=provider,
            instruction=system_instruction,
            policy=execution_policy,
            approver=execution_approver,
            parallel_commands=frozenset(parallel_commands or ()),
            parallel_tools=_validate_parallel_tool_names(
                parallel_tools or frozenset()
            ),
        )

    @classmethod
    async def _reconcile(
        cls,
        *,
        workspace: str | Path,
        repertoire: str | Path | None,
        provider: ModelProvider,
        instruction: str | None,
        policy: ExecutionPolicy | None,
        approver: ExecutionApprover | None,
        parallel_commands: frozenset[str],
        parallel_tools: frozenset[str],
    ) -> AgentRuntime:
        """Prepare Workspace-scoped resources and construct the Runtime."""

        paths = _prepare_workspace(workspace)
        base_env = _load_workspace_env(paths.environment)
        capability_view = _CapabilityView.open(paths.root, repertoire)
        tool_catalog = _ToolCatalog.reconcile(capability_view)
        tool_environment = await _ToolEnvironment.reconcile(capability_view)
        effective_policy = ExecutablePolicy() if policy is None else policy
        approval_gate = (
            None if approver is None else _ExecutionApprovalGate(approver)
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

        Args:
            session_id (`str`):
                Host-visible Session identifier. Reusing an id that was closed
                creates a fresh Loop and Kernel.
            message (`UserMessage`):
                User-authored message to append to the Session history.
            provider (`ModelProvider | None`):
                Optional provider override; used only when ``session_id`` is
                first seen.

        Yields:
            Model events streamed by the Session's Agent Loop.
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
            parallel_commands=self._parallel_commands,
            parallel_tools=self._parallel_tools,
        )


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
