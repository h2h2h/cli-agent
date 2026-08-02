"""Runtime-owned Workspace resource aggregate and reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cli_agent.runtime._capability.mcp.catalog import _MCPCatalog
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.environment import _ToolEnvironment
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._capability.workspace import (
    _load_workspace_env,
    _prepare_workspace,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic


@dataclass(frozen=True, slots=True)
class _RuntimeResources:
    """Reference-stable aggregate of Workspace-lifetime Runtime resources.

    ``frozen`` only prevents field rebinding; referenced components continue
    to encapsulate their own mutable state. ``base_env`` is excluded from the
    representation so debug output never contains Workspace environment values.
    """

    workspace: Path
    base_env: Mapping[str, str] = field(repr=False)
    capability_view: _CapabilityView
    tool_catalog: _ToolCatalog
    tool_environment: _ToolEnvironment
    skill_catalog: _SkillCatalog


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
    base_env = _load_workspace_env(paths.environment)
    capability_view = _CapabilityView.open(paths.root, repertoire)
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
    return _RuntimeResources(
        workspace=paths.root,
        base_env=base_env,
        capability_view=capability_view,
        tool_catalog=tool_catalog,
        tool_environment=tool_environment,
        skill_catalog=skill_catalog,
    )
