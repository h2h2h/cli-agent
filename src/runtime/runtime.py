"""Host-facing Runtime lifecycle."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from types import TracebackType
from typing import Any

from runtime._environment import EnvironmentKernel
from runtime.model import ModelProvider


class RuntimeClosedError(RuntimeError):
    """Raised when work is requested from a closed Agent Runtime."""


class AgentRuntime:
    """Own Workspace-scoped resources for the host."""

    def __init__(
        self,
        *,
        default_provider: ModelProvider,
        environment: EnvironmentKernel,
    ) -> None:
        self._default_provider = default_provider
        self._environment = environment
        self._closed = False

    @classmethod
    def open(
        cls,
        *,
        workspace: str | Path,
        provider: ModelProvider,
    ) -> _AgentRuntimeOpener:
        """Prepare to asynchronously open a Workspace-bound Runtime."""

        return _AgentRuntimeOpener(cls, workspace, provider)

    @classmethod
    async def _open(
        cls,
        workspace: str | Path,
        provider: ModelProvider,
    ) -> AgentRuntime:
        environment = EnvironmentKernel(workspace)
        try:
            return cls(
                default_provider=provider,
                environment=environment,
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
        await self._environment.close()

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
    ) -> None:
        self._runtime_type = runtime_type
        self._workspace = workspace
        self._provider = provider
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
            )
        else:
            self._runtime._ensure_open()
        return self._runtime
