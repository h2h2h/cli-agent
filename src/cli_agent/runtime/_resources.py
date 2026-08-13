"""Runtime-owned Workspace resource aggregate and reconciliation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _BoundCapabilityView,
    _CapabilityState,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.local import _LocalBackend
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.mcp.catalog import _MCPCatalog
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.source import _prepare_capability_source
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._database.session_history import _SessionHistory
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime._project_instructions import (
    _load_project_instructions,
    _ProjectInstructions,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic


@dataclass(frozen=True, slots=True)
class _RuntimeResources:
    """Reference-stable aggregate of Workspace-lifetime Runtime resources.

    ``frozen`` only prevents field rebinding; referenced components continue
    to encapsulate their own mutable state. ``base_env`` is excluded from the
    representation so debug output never contains Workspace environment values.
    The aggregate owns the RFC-0012 close sequence: the Library worker, the
    Backend Workspace flush, and the Backend Workspace close.
    """

    workspace: Path
    backend: _BackendWorkspace
    base_env: Mapping[str, str] = field(repr=False)
    capability_view: _BoundCapabilityView
    project_instructions: _ProjectInstructions | None
    tool_catalog: _ToolCatalog
    skill_catalog: _SkillCatalog
    library_catalog: _LibraryCatalog
    session_history: _SessionHistory

    async def close(self) -> None:
        """Close Workspace-lifetime resources in reverse dependency order.

        The Library worker (and its state database) stops first, the Backend
        Workspace is flushed, then the Workspace and Capability State close.
        Every step is attempted so a failure cannot leak resources; the first
        failure is raised so the Host never assumes persistence succeeded.
        """

        errors: list[Exception] = []
        try:
            await self.library_catalog.close()
        except Exception as exc:
            errors.append(exc)
        try:
            await self.backend.flush()
        except Exception as exc:
            errors.append(exc)
        try:
            await self.backend.close()
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

    The RFC-0012 open order is fixed: Host sources, Backend Workspace and
    Bound View, Workspace project instructions, Workspace MCP, Tool Catalog,
    Backend Tool Runtime, Skill Catalog, Library Catalog. Any failure rolls
    back every already-opened resource in reverse order and re-raises the
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
        paths = _prepare_workspace(workspace)
        capability_source = _prepare_capability_source(repertoire, paths.state)
        backend = await _LocalBackend().open_workspace(
            source=_WorkspaceSource(root=paths.root, environment=paths.environment),
            capability_source=capability_source,
            capability_state=_CapabilityState(root=paths.state),
        )
        opened.add(backend.close)
        project_instructions = await _load_project_instructions(
            backend.filesystem,
            str(paths.root),
        )
        state_database = _StateDatabase.open()
        opened.add(lambda: _close_database(state_database))
        summary_cache = _SummaryCache(state_database)
        session_history = _SessionHistory(
            state_database,
            on_diagnostic=on_diagnostic,
        )
        await _MCPCatalog.reconcile(
            backend,
            on_diagnostic=on_diagnostic,
        )
        tool_catalog = await _ToolCatalog.reconcile(
            backend.capabilities,
            backend.filesystem,
            on_diagnostic=on_diagnostic,
        )
        await backend.reconcile_tool_runtime()
        skill_catalog = await _SkillCatalog.reconcile(
            backend.capabilities,
            backend.filesystem,
        )
        library_catalog = await _LibraryCatalog.reconcile(
            backend.capabilities,
            backend.filesystem,
            summary_cache,
        )
        return _RuntimeResources(
            workspace=paths.root,
            backend=backend,
            base_env=backend.workspace_environment,
            capability_view=backend.capabilities,
            project_instructions=project_instructions,
            tool_catalog=tool_catalog,
            skill_catalog=skill_catalog,
            library_catalog=library_catalog,
            session_history=session_history,
        )
    except BaseException:
        await opened.rollback()
        raise


async def _close_database(database: _StateDatabase) -> None:
    """Close one application state database from an async closer."""
    database.close()
