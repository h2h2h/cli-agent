"""Host-facing Runtime lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from runtime._agent_loop import AgentLoop
from runtime._environment import EnvironmentBinding, EnvironmentKernel
from runtime._system_message import assemble_system_message
from runtime.model import ModelEvent, ModelProvider, UserMessage


class RuntimeClosedError(RuntimeError):
    """Raised when work is requested from a closed Agent Runtime."""


@dataclass(slots=True)
class _Session:
    provider: ModelProvider
    environment: EnvironmentBinding
    loop: AgentLoop


class AgentRuntime:
    """Own Workspace-scoped resources for the host."""

    def __init__(
        self,
        *,
        default_provider: ModelProvider,
        environment: EnvironmentKernel,
        workspace: Path,
        system_instruction: str | None,
    ) -> None:
        self._default_provider = default_provider
        self._environment = environment
        self._workspace = workspace
        self._system_instruction = system_instruction
        self._sessions: dict[str, _Session] = {}
        self._closed = False

    @classmethod
    def open(
        cls,
        *,
        workspace: str | Path,
        provider: ModelProvider,
        system_instruction: str | None = None,
    ) -> _AgentRuntimeOpener:
        """Prepare to asynchronously open a Workspace-bound Runtime.

        ``system_instruction`` extends the canonical instruction assembled for
        each new Agent Session.
        """

        return _AgentRuntimeOpener(
            cls,
            workspace,
            provider,
            system_instruction,
        )

    @classmethod
    async def _open(
        cls,
        workspace: str | Path,
        provider: ModelProvider,
        system_instruction: str | None,
    ) -> AgentRuntime:
        environment = EnvironmentKernel(workspace)
        try:
            return cls(
                default_provider=provider,
                environment=environment,
                workspace=Path(workspace).resolve(),
                system_instruction=system_instruction,
            )
        except BaseException:
            await environment.close()
            raise

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
            await session.environment.close()
        await self._environment.close()

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
            session_provider = (
                provider if provider is not None else self._default_provider
            )
            environment = self._environment.create_binding()
            session = _Session(
                provider=session_provider,
                environment=environment,
                loop=AgentLoop(
                    session_provider,
                    environment,
                    system_message=assemble_system_message(
                        self._workspace,
                        self._system_instruction,
                    ),
                ),
            )
            self._sessions[session_id] = session

        async for event in session.loop.run(message):
            yield event

    async def close_session(self, session_id: str) -> None:
        """Close and forget one Agent Session idempotently."""

        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.environment.close()

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


class _AgentRuntimeOpener:
    """Await or enter one Agent Runtime open operation."""

    def __init__(
        self,
        runtime_type: type[AgentRuntime],
        workspace: str | Path,
        provider: ModelProvider,
        system_instruction: str | None,
    ) -> None:
        self._runtime_type = runtime_type
        self._workspace = workspace
        self._provider = provider
        self._system_instruction = system_instruction
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
            self._runtime = await self._runtime_type._open(
                self._workspace,
                self._provider,
                self._system_instruction,
            )
        else:
            self._runtime._ensure_open()
        return self._runtime
