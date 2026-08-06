"""Runtime-owned Workspace resource aggregate and reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _CapabilitySource,
    _CapabilityState,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.local import _LocalBackend
from cli_agent.runtime._capability.library.cache import _SummaryCache
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.mcp.catalog import _MCPCatalog
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.environment import _ToolEnvironment
from cli_agent.runtime._capability.view import _CapabilityView, _prepare_repertoire
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._state_db import _StateDatabase
from cli_agent.runtime.diagnostic import RuntimeDiagnostic


@dataclass(frozen=True, slots=True)
class _RuntimeResources:
    """Reference-stable aggregate of Workspace-lifetime Runtime resources.

    ``frozen`` only prevents field rebinding; referenced components continue
    to encapsulate their own mutable state. ``base_env`` is excluded from the
    representation so debug output never contains Workspace environment values.
    """

    workspace: Path
    backend: _BackendWorkspace
    base_env: Mapping[str, str] = field(repr=False)
    capability_view: _CapabilityView
    tool_catalog: _ToolCatalog
    tool_environment: _ToolEnvironment
    skill_catalog: _SkillCatalog
    library_catalog: _LibraryCatalog


async def _reconcile_runtime_resources(
    *,
    workspace: str | Path,
    repertoire: str | Path | None,
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
) -> _RuntimeResources:
    """Reconcile Workspace-lifetime resources in the established order.

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

    paths = _prepare_workspace(workspace)
    repertoire_root = _prepare_repertoire(repertoire)
    backend = await _LocalBackend().open_workspace(
        source=_WorkspaceSource(root=paths.root, environment=paths.environment),
        capability_source=_CapabilitySource(repertoire=repertoire_root),
        capability_state=_CapabilityState(root=paths.state),
    )
    capability_view = _CapabilityView.open(paths.root, repertoire_root)
    state_database = _StateDatabase.open()
    summary_cache = _SummaryCache(state_database)
    await _MCPCatalog.reconcile(
        capability_view,
        on_diagnostic=on_diagnostic,
    )
    tool_catalog = _ToolCatalog.reconcile(
        capability_view,
        on_diagnostic=on_diagnostic,
    )
    tool_environment = await _ToolEnvironment.reconcile(capability_view)
    skill_catalog = _SkillCatalog.reconcile(capability_view)
    library_catalog = await _LibraryCatalog.reconcile(
        capability_view,
        summary_cache,
    )
    return _RuntimeResources(
        workspace=paths.root,
        backend=backend,
        base_env=backend.workspace_environment,
        capability_view=capability_view,
        tool_catalog=tool_catalog,
        tool_environment=tool_environment,
        skill_catalog=skill_catalog,
        library_catalog=library_catalog,
    )
