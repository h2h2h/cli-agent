"""Backend-neutral logical Workspace ports consumed by the Runtime core."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from cli_agent.runtime._backend import (
    Backend,
    _ShellExecutionRequest,
    _WorkspaceFilesystem,
)
from cli_agent.runtime._execution import ExecutionHandle
from cli_agent.runtime._project_instructions import _ProjectInstructions


class Workspace(Protocol):
    """One logical working environment with one stable Backend binding."""

    @property
    def id(self) -> str:
        """Return the stable Workspace identity."""
        ...

    @property
    def root(self) -> str:
        """Return the Backend-native Workspace root."""
        ...

    @property
    def base_environment(self) -> Mapping[str, str]:
        """Return explicit Workspace environment configuration."""
        ...

    @property
    def repertoire(self) -> Path:
        """Return the Host-owned capability lower tree."""
        ...

    @property
    def deployment_volume(self) -> str:
        """Return the Backend-relative capability artifact root."""
        ...

    @property
    def filesystem(self) -> _WorkspaceFilesystem:
        """Return the bound Workspace filesystem."""
        ...

    @property
    def backend(self) -> Backend:
        """Return the stable Backend binding."""
        ...

    def prepare_shell(self, request: _ShellExecutionRequest) -> ExecutionHandle:
        """Prepare one shell execution through the bound Backend."""
        ...

    def execution_base_environment(self) -> Mapping[str, str]:
        """Return the complete environment for child execution."""
        ...

    async def load_project_instructions(self) -> _ProjectInstructions | None:
        """Load validated effective project instructions."""
        ...

    async def flush(self) -> None:
        """Persist pending changes through the bound Backend."""
        ...

    async def close(self) -> None:
        """Close the bound Backend idempotently."""
        ...


class WorkspaceFactory(Protocol):
    """Open Workspaces from Host-selected configuration."""

    async def open(
        self,
        workspace: str | Path,
        *,
        repertoire: str | Path | None,
    ) -> Workspace:
        """Open one Workspace; any failure must fail closed."""
        ...
