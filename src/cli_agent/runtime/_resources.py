"""Runtime-owned Workspace resource aggregate and reconciliation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cli_agent.runtime._backend import _BackendWorkspace, _BoundCapabilityView
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.mcp.catalog import _MCPCatalog
from cli_agent.runtime._capability.projections import write_catalog_indexes
from cli_agent.runtime._capability.provider import (
    CapabilityProvider,
    CapabilitySnapshot,
)
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime._workspace import Workspace, _LocalWorkspaceFactory
from cli_agent.runtime.diagnostic import RuntimeDiagnostic


@dataclass(frozen=True, slots=True)
class _RuntimeResources:
    """Reference-stable aggregate of Workspace-lifetime Runtime resources.

    ``frozen`` only prevents field rebinding; referenced components continue
    to encapsulate their own mutable state. ``base_env`` is excluded from the
    representation so debug output never contains Workspace environment values.
    The aggregate owns the RFC-0012 close sequence: the Library worker stops
    first, then the Workspace flushes and closes its bound Backend.
    """

    workspace: Workspace
    backend: _BackendWorkspace
    base_env: Mapping[str, str] = field(repr=False)
    capability_view: _BoundCapabilityView
    snapshot: CapabilitySnapshot
    session_store: SessionStore

    async def close(self) -> None:
        """Close Workspace-lifetime resources in reverse dependency order.

        The Library worker (and its state database) stops first, the Backend
        Workspace is flushed, then the Workspace closes its Backend. Every
        step is attempted so a failure cannot leak resources; the first
        failure is raised so the Host never assumes persistence succeeded.
        """

        errors: list[Exception] = []
        library = self.snapshot.library
        if library is not None:
            try:
                await library.close()
            except Exception as exc:
                errors.append(exc)
        try:
            await self.backend.flush()
        except Exception as exc:
            errors.append(exc)
        try:
            await self.workspace.close()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise errors[0]


class _OpenResources:
    """Best-effort reverse-order closer for a partially opened Runtime.

    Tracks closers as resources are acquired during Runtime open; on any
    failure every already-opened resource is closed, and close failures are
    swallowed so the original open failure reaches the caller.
    """

    def __init__(self) -> None:
        self._closers: list[Callable[[], Awaitable[None]]] = []

    def add(self, closer: Callable[[], Awaitable[None]]) -> None:
        """Register one closer for an opened resource."""
        self._closers.append(closer)

    async def rollback(self) -> None:
        """Close every opened resource in reverse order, best effort."""
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:
                pass


async def _reconcile_runtime_resources(
    *,
    workspace: str | Path,
    repertoire: str | Path | None,
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
) -> _RuntimeResources:
    """Reconcile Workspace-lifetime resources in the established order.

    The RFC-0012 open order is fixed: Workspace identity, Host sources,
    Backend Workspace and Bound View, MCP config discovery and stub
    projection, Capability snapshot discovery, catalog index projections,
    Backend Tool Runtime, and the Library Catalog. Any failure rolls back
    every already-opened resource in reverse order and re-raises the
    original failure.

    Args:
        workspace (`str | Path`):
            Existing directory to bind as the Workspace.
        repertoire (`str | Path | None`):
            User-maintained capability lower tree.
        on_diagnostic (`Callable[[RuntimeDiagnostic], None] | None`):
            Optional Host callback for non-blocking reconcile notices.

    Returns:
        The reconciled resource aggregate.

    Raises:
        ValueError: If Workspace preparation or environment loading fails.
    """

    opened = _OpenResources()
    try:
        opened_workspace = await _LocalWorkspaceFactory().open(
            workspace,
            repertoire=repertoire,
        )
        opened.add(opened_workspace.close)
        backend = opened_workspace.backend
        provider = CapabilityProvider(
            view=opened_workspace.capability_source,
            workspace=opened_workspace.root_path,
            on_diagnostic=on_diagnostic,
        )
        mcp_configs = await provider.discover_mcp_configs()
        await _MCPCatalog.reconcile(
            backend,
            on_diagnostic,
            configs=mcp_configs,
        )
        snapshot = await provider.discover(mcp_configs=mcp_configs)
        await write_catalog_indexes(
            view_root=backend.capabilities.root,
            filesystem=backend.filesystem,
            snapshot=snapshot,
        )
        await backend.reconcile_tool_runtime()
        state_database = _StateDatabase.open()
        opened.add(lambda: _close_database(state_database))
        summary_cache = _SummaryCache(state_database)
        session_store = SessionStore(state_database)
        library_catalog = await _LibraryCatalog.reconcile(
            backend.capabilities,
            backend.filesystem,
            summary_cache,
        )
        snapshot = snapshot.with_library(library_catalog)
        return _RuntimeResources(
            workspace=opened_workspace,
            backend=backend,
            base_env=backend.workspace_environment,
            capability_view=backend.capabilities,
            snapshot=snapshot,
            session_store=session_store,
        )
    except BaseException:
        await opened.rollback()
        raise


async def _close_database(database: _StateDatabase) -> None:
    """Close one application state database from an async closer."""
    database.close()
