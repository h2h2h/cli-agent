"""Local CapabilityDeployment: materialize snapshots into a Local Workspace.

The default deployment implementation connects the control plane to a
Workspace (RFC-0014): it attaches the Local Capability View, discovers MCP
servers, projects generated stubs and the invocation binding, publishes
catalog index projections and the Runtime-owned worker, keeps the private
dependency environment in sync, and records one completion manifest that
binds the published artifacts to the snapshot revision and the Workspace
identity.

All artifact writes address Backend-relative capability volume paths
through the Workspace filesystem; only the inherently Local mechanics
(symlink view attach, venv creation, dependency synchronization, MCP
connections) touch Host paths. Every artifact lives under the persistent
capability volume, never inside an ExecutionHandle lifetime.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from cli_agent.runtime._backend.local.backend import _LocalBackendWorkspace
from cli_agent.runtime._backend.local.executor import _LocalToolExecutor
from cli_agent.runtime._backend.local.mcp_runtime import discover_servers
from cli_agent.runtime._backend.local.tool_runtime import (
    effective_requirements,
    ensure_tool_runtime,
    worker_template,
)
from cli_agent.runtime._backend.local.view import _LocalCapabilityView
from cli_agent.runtime._capability.deployment import (
    DEPLOYMENT_MANIFEST,
    DEPLOYMENT_SCHEMA_VERSION,
    TOOL_RUNTIME_DIRECTORY,
    DeploymentSnapshot,
    ToolExecutor,
    _DeploymentManifest,
    commit_manifest,
    publish_domains,
    read_manifest,
    volume_path,
)
from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    _MCPServerFacts,
)
from cli_agent.runtime._capability.mcp.stubs import materialize_stubs
from cli_agent.runtime._capability.projections import render_catalog_indexes
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime._capability.workspace import _ensure_real_directory
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

if TYPE_CHECKING:
    from cli_agent.runtime._capability.provider import CapabilitySnapshot
    from cli_agent.runtime._workspace import Workspace

_WORKER_FILENAME = "worker.py"
_EFFECTIVE_REQUIREMENTS = "effective-requirements.txt"
_DEPLOY_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[Path, asyncio.Lock],
] = weakref.WeakKeyDictionary()


class _LocalCapabilityDeployment:
    """Default Local deployment between CapabilitySnapshots and a Workspace."""

    def __init__(
        self,
        *,
        state_root: Path,
        repertoire: Path,
        volume: str,
        base_environment: Callable[[], Mapping[str, str]],
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> None:
        self._state_root = state_root
        self._repertoire = repertoire
        self._volume = volume
        self._base_environment = base_environment
        self._on_diagnostic = on_diagnostic
        self._manifest: _DeploymentManifest | None = None
        self._manifest_loaded = False
        self._realized: dict[str, str] = {}
        self._deployment: DeploymentSnapshot | None = None

    def executor(
        self,
        workspace: Workspace,
        *,
        revision: str,
    ) -> ToolExecutor:
        """Return the ToolExecutor bound to the latest reconciled deployment.

        The executor validates the active deployment before every Tool
        execution; a Runtime-open reconcile failure is stored as an
        incomplete deployment so Tool runs fail classified instead of
        running a stale worker.
        """

        local_backend = workspace.backend
        if not isinstance(local_backend, _LocalBackendWorkspace):
            raise ValueError(
                "Local ToolExecutor requires a Local Backend Workspace",
            )
        deployment = self._deployment or DeploymentSnapshot(
            workspace_id=workspace.id,
            revision=revision,
            layout_version=DEPLOYMENT_SCHEMA_VERSION,
            complete=False,
            error="Tool environment is unavailable",
        )
        return _LocalToolExecutor(
            local_backend,
            workspace_id=workspace.id,
            revision=revision,
            deployment=deployment,
        )

    async def attach(
        self,
        workspace: Workspace,
    ) -> _LogicalCapabilityView:
        """Materialize and bind the Local Capability View (idempotent).

        Attaching fills View gaps and removes stale lower links without
        disturbing Workspace-owned upper files or whiteouts, then binds the
        View to the Backend Workspace so filesystem copy-up and Shell
        mutation preparation observe capability mutations.
        """

        view = _LocalCapabilityView.materialize(
            state_root=self._state_root,
            repertoire=self._repertoire,
        )
        local_backend = workspace.backend
        if not isinstance(local_backend, _LocalBackendWorkspace):
            raise ValueError(
                "Local deployment requires a Local Backend Workspace",
            )
        local_backend._bind_capability_view(view)
        return view

    async def discover_mcp(
        self,
        configs: tuple[MCPServerConfig, ...],
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> tuple[_MCPServerFacts, ...]:
        """Discover Workspace MCP servers from the Local execution context."""

        return await discover_servers(
            configs,
            self._base_environment(),
            on_diagnostic or self._on_diagnostic,
        )

    async def materialize_stubs(
        self,
        workspace: Workspace,
        configs: tuple[MCPServerConfig, ...],
        facts: tuple[_MCPServerFacts, ...],
    ) -> None:
        """Project generated MCP stubs and the invocation binding.

        Unchanged stub and binding domains are skipped via the completion
        manifest. A binding materialization failure keeps the previous
        deployment in place, emits one ``mcp.binding_failed`` diagnostic,
        and skips stub projection for this round.
        """

        async with self._deployment_lock():
            manifest = await self._load_manifest(workspace)
            realized = self._realized_digests(manifest)
            realized, _ = await materialize_stubs(
                filesystem=workspace.filesystem,
                volume=self._volume,
                workspace_id=workspace.id,
                configs=configs,
                facts=facts,
                manifest=manifest,
                realized=realized,
                on_diagnostic=self._on_diagnostic,
            )
            self._realized = realized

    async def reconcile(
        self,
        snapshot: CapabilitySnapshot,
        workspace: Workspace,
    ) -> DeploymentSnapshot:
        """Deploy the snapshot into the Workspace idempotently.

        Catalog index projections, the Runtime-owned worker, and the
        effective requirements are republished only when their artifact
        domains changed. The private dependency environment is kept in
        sync by its own digest marker. A failure in the Tool environment
        keeps the previous complete deployment on disk and returns an
        incomplete DeploymentSnapshot instead of raising; the completion
        manifest is only rewritten after every domain materialized.
        """

        async with self._deployment_lock():
            manifest = await self._load_manifest(workspace)
            realized = self._realized_digests(manifest)
            indexes = {
                volume_path(self._volume, key): content
                for key, content in render_catalog_indexes(
                    snapshot=snapshot,
                ).items()
            }
            realized = await publish_domains(
                filesystem=workspace.filesystem,
                workspace_id=workspace.id,
                manifest=manifest,
                realized=realized,
                domains={"indexes": indexes},
            )

            runtime = None
            error: str | None = None
            try:
                tool_root = self._state_root / TOOL_RUNTIME_DIRECTORY
                _ensure_real_directory(tool_root, label="Tool environment path")
                requirements = await self._read_requirements(workspace)
                effective = effective_requirements(requirements)
                worker = worker_template()
                tool_domains = {
                    "worker": {
                        volume_path(
                            self._volume,
                            TOOL_RUNTIME_DIRECTORY,
                            _WORKER_FILENAME,
                        ): worker,
                    },
                    "requirements": {
                        volume_path(
                            self._volume,
                            TOOL_RUNTIME_DIRECTORY,
                            _EFFECTIVE_REQUIREMENTS,
                        ): effective,
                    },
                }
                realized = await publish_domains(
                    filesystem=workspace.filesystem,
                    workspace_id=workspace.id,
                    manifest=manifest,
                    realized=realized,
                    domains=tool_domains,
                )
                runtime = await ensure_tool_runtime(
                    tool_root,
                    tools_directory=self._state_root / "tools",
                    effective_content=effective,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"Tool environment is unavailable: {exc}"

            if runtime is None or not runtime.available:
                reason = (
                    error
                    or (runtime.error if runtime is not None else None)
                    or "Tool environment is unavailable"
                )
                if runtime is not None:
                    local_backend = workspace.backend
                    if isinstance(local_backend, _LocalBackendWorkspace):
                        local_backend._attach_tool_runtime(runtime)
                snapshot_result = DeploymentSnapshot(
                    workspace_id=workspace.id,
                    revision=snapshot.revision,
                    layout_version=DEPLOYMENT_SCHEMA_VERSION,
                    complete=False,
                    error=reason,
                )
                self._deployment = snapshot_result
                return snapshot_result

            await commit_manifest(
                workspace.filesystem,
                self._volume,
                workspace_id=workspace.id,
                revision=snapshot.revision,
                realized=realized,
                previous=manifest,
            )
            local_backend = workspace.backend
            if isinstance(local_backend, _LocalBackendWorkspace):
                local_backend._attach_tool_runtime(runtime)
            snapshot_result = DeploymentSnapshot(
                workspace_id=workspace.id,
                revision=snapshot.revision,
                layout_version=DEPLOYMENT_SCHEMA_VERSION,
                complete=True,
                error=None,
            )
            self._deployment = snapshot_result
            return snapshot_result

    async def _load_manifest(
        self,
        workspace: Workspace,
    ) -> _DeploymentManifest | None:
        """Read the completion manifest once per deployment instance."""

        if not self._manifest_loaded:
            self._manifest = await read_manifest(
                workspace.filesystem,
                volume_path(self._volume, DEPLOYMENT_MANIFEST),
            )
            self._manifest_loaded = True
        return self._manifest

    def _realized_digests(
        self,
        manifest: _DeploymentManifest | None,
    ) -> dict[str, str]:
        """Return the digests realized so far in this open."""

        if self._realized:
            return self._realized
        return dict(manifest.digests) if manifest is not None else {}

    async def _read_requirements(self, workspace: Workspace) -> bytes:
        """Read the effective user Tool requirements through the Workspace."""

        try:
            return await workspace.filesystem.read(
                volume_path(self._volume, "tools", "requirements.txt"),
            )
        except _FilesystemError:
            return b""

    def _deployment_lock(self) -> asyncio.Lock:
        """Return the per-Workspace deployment lock for this event loop."""

        loop = asyncio.get_running_loop()
        locks = _DEPLOY_LOCKS.setdefault(loop, {})
        return locks.setdefault(self._state_root, asyncio.Lock())

    def _emit(
        self,
        kind: str,
        message: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        if self._on_diagnostic is None:
            return
        self._on_diagnostic(
            RuntimeDiagnostic(kind=kind, message=message, detail=detail or {}),
        )
