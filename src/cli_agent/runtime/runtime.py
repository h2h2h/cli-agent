"""Host-facing Runtime lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from cli_agent.runtime._agent_loop import AgentLoop
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.policy import (
    DirectExecutableDenyPolicy,
    ExecutionPolicy,
)
from cli_agent.runtime._environment.scheduler import (
    _DEFAULT_PARALLEL_LIMIT,
    _DEFAULT_QUEUE_LIMIT,
    _validate_parallel_limit,
    _validate_queue_limit,
)
from cli_agent.runtime._system_message import assemble_system_message
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
        base_env: Mapping[str, str],
        policy: ExecutionPolicy,
        queue_limit: int,
        parallel_limit: int,
        parallel_commands: frozenset[str],
        instruction: str | None,
    ) -> None:
        self._provider = provider
        self._workspace = workspace
        self._base_env = base_env
        self._policy = policy
        self._queue_limit = queue_limit
        self._parallel_limit = parallel_limit
        self._parallel_commands = parallel_commands
        self._instruction = instruction
        self._sessions: dict[str, _Session] = {}
        self._closed = False

    @classmethod
    def open(
        cls,
        *,
        workspace: str | Path,
        provider: ModelProvider,
        system_instruction: str | None = None,
        denied_executables: frozenset[str] | None = None,
        pending_execution_capacity: int = _DEFAULT_QUEUE_LIMIT,
        parallel_execution_capacity: int = _DEFAULT_PARALLEL_LIMIT,
        parallel_shell_commands: frozenset[str] | None = None,
    ) -> _AgentRuntimeOpener:
        """Prepare to asynchronously open a Workspace-bound Runtime.

        ``system_instruction`` extends the canonical instruction assembled for
        each new Agent Session. ``denied_executables`` replaces the default
        direct-executable deny set containing ``rm`` for this Runtime lifetime.
        ``pending_execution_capacity`` bounds queued Executions in each Session
        and defaults to 32. ``parallel_execution_capacity`` bounds a batch of
        trusted parallel-safe commands; ``parallel_shell_commands`` grants that
        scheduling class to simple direct invocations of those executables.
        """

        return _AgentRuntimeOpener(
            cls,
            workspace,
            provider,
            system_instruction,
            denied_executables,
            _validate_queue_limit(pending_execution_capacity),
            _validate_parallel_limit(parallel_execution_capacity),
            frozenset(parallel_shell_commands or ()),
        )

    @classmethod
    async def _open(
        cls,
        workspace: str | Path,
        provider: ModelProvider,
        instruction: str | None,
        denied: frozenset[str] | None,
        queue_limit: int,
        parallel_limit: int,
        parallel_commands: frozenset[str],
    ) -> AgentRuntime:
        paths = _prepare_workspace(workspace)
        base_env = _load_workspace_environment(paths.environment)
        policy = (
            DirectExecutableDenyPolicy()
            if denied is None
            else DirectExecutableDenyPolicy(denied)
        )
        return cls(
            provider=provider,
            workspace=paths.root,
            base_env=base_env,
            policy=policy,
            queue_limit=queue_limit,
            parallel_limit=parallel_limit,
            parallel_commands=parallel_commands,
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
            kernel = self._new_kernel()
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

    def _new_kernel(self) -> EnvironmentKernel:
        return EnvironmentKernel(
            self._workspace,
            base_env=self._base_env,
            policy=self._policy,
            queue_limit=self._queue_limit,
            parallel_limit=self._parallel_limit,
            parallel_commands=self._parallel_commands,
        )


class _AgentRuntimeOpener:
    """Await or enter one Agent Runtime open operation."""

    def __init__(
        self,
        runtime_cls: type[AgentRuntime],
        workspace: str | Path,
        provider: ModelProvider,
        instruction: str | None,
        denied: frozenset[str] | None,
        queue_limit: int,
        parallel_limit: int,
        parallel_commands: frozenset[str],
    ) -> None:
        self._runtime_cls = runtime_cls
        self._workspace = workspace
        self._provider = provider
        self._instruction = instruction
        self._denied = denied
        self._queue_limit = queue_limit
        self._parallel_limit = parallel_limit
        self._parallel_commands = parallel_commands
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
                self._provider,
                self._instruction,
                self._denied,
                self._queue_limit,
                self._parallel_limit,
                self._parallel_commands,
            )
        else:
            self._runtime._ensure_open()
        return self._runtime
