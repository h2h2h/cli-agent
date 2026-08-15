"""Runtime-owned Workspace resource aggregate and reconciliation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from cli_agent.runtime._backend import _BackendWorkspace
from cli_agent.runtime._backend.docker.deployment import (
    _docker_project_instructions,
    _DockerCapabilityDeployment,
)
from cli_agent.runtime._backend.local.deployment import _LocalCapabilityDeployment
from cli_agent.runtime._capability.deployment import (
    DeploymentSnapshot,
    ToolExecutor,
)
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    _MCPServerFacts,
)
from cli_agent.runtime._capability.provider import (
    CapabilityProvider,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime._workspace import (
    Workspace,
    _DockerWorkspaceFactory,
    _LocalWorkspaceFactory,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic


class _RuntimeCapabilityDeployment(Protocol):
    """Complete deployment interface consumed by the composition root."""

    async def attach(self, workspace: Workspace) -> _LogicalCapabilityView:
        """Attach the effective Capability View."""
        ...

    async def discover_mcp(
        self,
        configs: tuple[MCPServerConfig, ...],
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> tuple[_MCPServerFacts, ...]:
        """Discover MCP facts in the deployment execution context."""
        ...

    async def materialize_stubs(
        self,
        workspace: Workspace,
        configs: tuple[MCPServerConfig, ...],
        facts: tuple[_MCPServerFacts, ...],
    ) -> None:
        """Publish MCP stubs and their invocation binding."""
        ...

    async def reconcile(
        self,
        snapshot: CapabilitySnapshot,
        workspace: Workspace,
    ) -> DeploymentSnapshot:
        """Reconcile a snapshot into the Workspace."""
        ...

    def executor(
        self,
        workspace: Workspace,
        *,
        revision: str,
    ) -> ToolExecutor:
        """Return the executor bound to the latest reconcile result."""
        ...


class _WorkspaceEnvironment(Protocol):
    """Backend-private environment fact consumed by Runtime composition."""

    @property
    def workspace_environment(self) -> Mapping[str, str]:
        """Return Workspace variables without Host ambient values."""
        ...


@dataclass(frozen=True, slots=True)
class _RuntimeResources:
    """Reference-stable aggregate of Workspace-lifetime Runtime resources.

    ``frozen`` only prevents field rebinding; referenced components continue
    to encapsulate their own mutable state. ``base_env`` is excluded from
    the representation so debug output never contains Workspace environment
    values. The aggregate owns the RFC-0012 close sequence: the Library
    worker stops first, then the Workspace flushes and closes its bound
    Backend.
    """

    workspace: Workspace
    backend: _BackendWorkspace
    base_env: Mapping[str, str] = field(repr=False)
    capability_view: _LogicalCapabilityView
    snapshot: CapabilitySnapshot
    deployment: DeploymentSnapshot
    tool_executor: ToolExecutor
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
    backend: str = "local",
) -> _RuntimeResources:
    """Reconcile Workspace-lifetime resources in the established order.

    The RFC-0014 open order is fixed: Workspace identity and Host sources,
    Backend Workspace open, CapabilityDeployment view attach, MCP config
    discovery, MCP server discovery and stub projection, Capability
    snapshot discovery, the application state database, the Library
    Catalog, and finally capability deployment reconcile — so the deployed
    manifest always binds the snapshot revision that includes the Library
    fingerprints (the same revision every executor and Session consumes).
    Any failure rolls back every already-opened resource in reverse order
    and re-raises the original failure; a Tool environment deployment
    failure is fail-soft and surfaces through the DeploymentSnapshot
    instead.

    The Backend is fixed at open time and never hot-swapped: ``backend``
    selects the Workspace Factory and CapabilityDeployment plane
    (``"local"`` or ``"docker"``) that own the whole Workspace lifetime.

    Args:
        workspace (`str | Path`):
            Existing directory to bind as the Workspace (Host control
            directory for Docker).
        repertoire (`str | Path | None`):
            User-maintained capability lower tree.
        on_diagnostic (`Callable[[RuntimeDiagnostic], None] | None`):
            Optional Host callback for non-blocking reconcile notices.
        backend (`str`):
            The Workspace Backend kind, fixed for the Workspace lifetime.

    Returns:
        The reconciled resource aggregate.

    Raises:
        ValueError: If Workspace preparation or environment loading fails,
            or the requested Backend kind is unknown.
    """

    opened = _OpenResources()
    try:
        opened_workspace: Workspace
        deployment: _RuntimeCapabilityDeployment
        view: _LogicalCapabilityView
        if backend == "docker":
            docker_workspace = await _DockerWorkspaceFactory().open(
                workspace,
                repertoire=repertoire,
            )
            opened.add(docker_workspace.close)
            docker_deployment = _DockerCapabilityDeployment(
                state_root=docker_workspace.state_root,
                repertoire=docker_workspace.repertoire,
                volume=docker_workspace.deployment_volume,
                backend=docker_workspace.backend,
                on_diagnostic=on_diagnostic,
            )
            view = await docker_deployment.attach(docker_workspace)
            provider = CapabilityProvider(
                view=view,
                workspace=Path("/workspace"),
                instructions_loader=lambda: _docker_project_instructions(
                    docker_workspace.backend
                ),
                on_diagnostic=on_diagnostic,
            )
            opened_workspace = docker_workspace
            deployment = docker_deployment
        elif backend == "local":
            local_workspace = await _LocalWorkspaceFactory().open(
                workspace,
                repertoire=repertoire,
            )
            opened.add(local_workspace.close)
            local_deployment = _LocalCapabilityDeployment(
                state_root=local_workspace.state_root,
                repertoire=local_workspace.repertoire,
                volume=local_workspace.deployment_volume,
                base_environment=local_workspace.backend.execution_base_environment,
                on_diagnostic=on_diagnostic,
            )
            view = await local_deployment.attach(local_workspace)
            provider = CapabilityProvider(
                view=view,
                workspace=local_workspace.root_path,
                on_diagnostic=on_diagnostic,
            )
            opened_workspace = local_workspace
            deployment = local_deployment
        else:
            raise ValueError(f"unsupported Backend kind: {backend}")
        backend_workspace = opened_workspace.backend
        mcp_configs = await provider.discover_mcp_configs()
        mcp_facts = await deployment.discover_mcp(mcp_configs)
        await deployment.materialize_stubs(
            opened_workspace,
            mcp_configs,
            mcp_facts,
        )
        snapshot = await provider.discover(mcp_configs=mcp_configs)
        state_database = _StateDatabase.open()
        opened.add(lambda: _close_database(state_database))
        summary_cache = _SummaryCache(state_database)
        session_store = SessionStore(state_database)
        library_catalog = await _LibraryCatalog.reconcile(
            view,
            backend_workspace.filesystem,
            summary_cache,
        )
        snapshot = snapshot.with_library(library_catalog)
        deployment_snapshot = await deployment.reconcile(
            snapshot,
            opened_workspace,
        )
        return _RuntimeResources(
            workspace=opened_workspace,
            backend=backend_workspace,
            base_env=cast(
                _WorkspaceEnvironment,
                backend_workspace,
            ).workspace_environment,
            capability_view=view,
            snapshot=snapshot,
            deployment=deployment_snapshot,
            tool_executor=deployment.executor(
                opened_workspace,
                revision=snapshot.revision,
            ),
            session_store=session_store,
        )
    except BaseException:
        await opened.rollback()
        raise


async def _close_database(database: _StateDatabase) -> None:
    """Close one application state database from an async closer."""
    database.close()
