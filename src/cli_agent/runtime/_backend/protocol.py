"""Backend Workspace execution and filesystem contracts.

These private protocols are the Backend-neutral seam of RFC-0012: command
Handlers, Capability Catalogs, and cwd validation depend on these contracts
without reading a concrete Backend type. ``prepare_shell`` and
``prepare_tool`` are synchronous and free of external side effects; resource
creation is deferred to the returned ``ExecutionHandle.run()``.

Capability materialization is NOT part of the Backend contract: the
CapabilityDeployment plane owns Capability View attach, Tool worker and
dependency materialization, and MCP bindings (RFC-0014).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cli_agent.runtime._backend.facts import (
    _DirectoryEntry,
    _FileEditRequest,
    _FileEditResult,
    _FileMetadata,
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
    _ShellExecutionRequest,
    _ToolExecutionRequest,
    _WorkspaceSource,
)
from cli_agent.runtime._execution import ExecutionHandle


@runtime_checkable
class _Backend(Protocol):
    """Open one Backend Workspace from Host-side sources."""

    async def open_workspace(
        self,
        source: _WorkspaceSource,
    ) -> _BackendWorkspace:
        """Open a live Workspace; any open failure must fail closed."""
        ...


@runtime_checkable
class _BackendWorkspace(Protocol):
    """One live Runtime-owned Workspace shared by every Session Kernel."""

    root: str
    filesystem: _WorkspaceFilesystem

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> ExecutionHandle:
        """Prepare one Shell execution without starting work or resources."""
        ...

    def prepare_tool(
        self,
        request: _ToolExecutionRequest,
    ) -> ExecutionHandle:
        """Prepare one Tool worker execution without starting work."""
        ...

    async def flush(self) -> None:
        """Persist pending Workspace changes; failures must surface."""
        ...

    async def close(self) -> None:
        """Close Backend resources and forbid further Workspace use."""
        ...


@runtime_checkable
class _WorkspaceFilesystem(Protocol):
    """Async Workspace filesystem shared by commands, Tools, and Catalogs."""

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        """Resolve one path using Backend-native semantics without I/O."""
        ...

    async def stat(self, path: str) -> _FileMetadata:
        """Return backend-neutral facts for one Workspace path."""
        ...

    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]:
        """Return sorted backend-neutral entries for one directory."""
        ...

    async def read(self, path: str) -> bytes:
        """Read one regular file and return its raw bytes."""
        ...

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        """Commit one atomic write in the Workspace namespace."""
        ...

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        """Commit one atomic read-modify-write in the Workspace namespace."""
        ...

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """Remove one file or directory from the Workspace namespace."""
        ...
