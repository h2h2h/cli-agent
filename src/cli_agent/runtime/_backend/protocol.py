"""Backend Workspace execution and filesystem contracts.

These private protocols are the Backend-neutral seam of RFC-0012: command
Handlers, Capability Catalogs, and cwd validation depend on these contracts
without reading a concrete Backend type. ``prepare_shell`` and
``prepare_tool`` are synchronous and free of external side effects; resource
creation is deferred to the returned ``_PreparedExecution.run()``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from cli_agent.runtime._backend.facts import (
    _CapabilityInspection,
    _CapabilitySource,
    _CapabilityState,
    _DirectoryEntry,
    _FileEditRequest,
    _FileEditResult,
    _FileMetadata,
    _FileWriteRequest,
    _FileWriteResult,
    _MCPServerFacts,
    _ResolvedPath,
    _ShellExecutionRequest,
    _ToolExecutionRequest,
    _ToolRuntimeStatus,
    _WorkspaceSource,
)
from cli_agent.runtime._capability.mcp.facts import MCPServerConfig
from cli_agent.runtime._environment.handlers.base import _PreparedExecution
from cli_agent.runtime.diagnostic import RuntimeDiagnostic


@runtime_checkable
class _Backend(Protocol):
    """Open one Backend Workspace from Host-side sources."""

    async def open_workspace(
        self,
        source: _WorkspaceSource,
        capability_source: _CapabilitySource,
        capability_state: _CapabilityState,
    ) -> _BackendWorkspace:
        """Open a live Workspace; any open failure must fail closed."""
        ...


@runtime_checkable
class _BackendWorkspace(Protocol):
    """One live Runtime-owned Workspace shared by every Session Kernel."""

    root: str
    filesystem: _WorkspaceFilesystem
    capabilities: _BoundCapabilityView
    mcp: _WorkspaceMCPRuntime

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> _PreparedExecution:
        """Prepare one Shell execution without starting work or resources."""
        ...

    def prepare_tool(
        self,
        request: _ToolExecutionRequest,
    ) -> _PreparedExecution:
        """Prepare one Tool worker execution without starting work."""
        ...

    async def reconcile_tool_runtime(self) -> _ToolRuntimeStatus:
        """Reconcile the Workspace Tool Runtime and return availability."""
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


@runtime_checkable
class _BoundCapabilityView(Protocol):
    """Effective Capability View materialized inside one Backend Workspace."""

    root: str

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        """Return provenance and shadow facts for one managed path."""
        ...

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        """Return sorted effective entries for one managed directory."""
        ...

    async def read(self, relative_path: str) -> bytes:
        """Read one managed file from the effective view."""
        ...

    async def stat(self, relative_path: str) -> _FileMetadata:
        """Return effective metadata for one managed path."""
        ...


@runtime_checkable
class _WorkspaceMCPRuntime(Protocol):
    """Discover Workspace MCP servers and own their invocation binding.

    Discovery and invocation both live inside the Backend Workspace: the
    Runtime returns provider-neutral server/tool facts and never exposes a
    transport stream, client, or subprocess; the invocation binding is
    materialized into the Backend Tool Runtime for the worker to call.
    """

    async def discover(
        self,
        configs: tuple[MCPServerConfig, ...],
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> tuple[_MCPServerFacts, ...]:
        """Return provider-neutral facts for every successfully connected server."""
        ...

    async def materialize_binding(
        self,
        configs: tuple[MCPServerConfig, ...],
    ) -> None:
        """Materialize the worker-side invocation binding for the given servers."""
        ...
