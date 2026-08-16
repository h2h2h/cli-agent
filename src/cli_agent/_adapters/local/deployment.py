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
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from cli_agent._adapters.local.tool_runtime import (
    _LocalToolRuntime,
    effective_requirements,
    ensure_tool_runtime,
    worker_template,
)
from cli_agent.runtime._capability.deployment import (
    DEPLOYMENT_MANIFEST,
    DEPLOYMENT_SCHEMA_VERSION,
    TOOL_RUNTIME_DIRECTORY,
    DeploymentSnapshot,
    ToolRuntimeSnapshot,
    _DeploymentManifest,
    commit_manifest,
    publish_domains,
    read_manifest,
    volume_path,
)
from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.mcp.stubs import materialize_stubs
from cli_agent.runtime._capability.projections import render_catalog_indexes
from cli_agent.runtime._capability.workspace import _ensure_real_directory
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.host import NULL_EVENTS, EventSink, emit_event

if TYPE_CHECKING:
    from cli_agent.runtime._capability.snapshot import CapabilitySnapshot
    from cli_agent.runtime._workspace import Workspace

_WORKER_FILENAME = "worker.py"
_EFFECTIVE_REQUIREMENTS = "effective-requirements.txt"
_DEPLOY_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Lock],
] = weakref.WeakKeyDictionary()


class _LocalCapabilityDeployment:
    """Default Local deployment between CapabilitySnapshots and a Workspace."""

    def __init__(
        self,
        *,
        events: EventSink = NULL_EVENTS,
    ) -> None:
        self._events = events
        self._manifest: _DeploymentManifest | None = None
        self._manifest_loaded = False
        self._realized: dict[str, str] = {}

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

        volume = workspace.deployment_volume
        state_root = Path(workspace.root) / volume
        async with self._deployment_lock(workspace.id):
            manifest = await self._load_manifest(workspace)
            realized = self._realized_digests(manifest)
            realized, _ = await materialize_stubs(
                filesystem=workspace.filesystem,
                volume=volume,
                workspace_id=workspace.id,
                configs=snapshot.mcp_servers,
                facts=snapshot.mcp_facts,
                manifest=manifest,
                realized=realized,
                events=self._events,
            )
            indexes = {
                volume_path(volume, key): content
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
                tool_root = state_root / TOOL_RUNTIME_DIRECTORY
                _ensure_real_directory(tool_root, label="Tool environment path")
                requirements = await self._read_requirements(workspace)
                effective = effective_requirements(requirements)
                worker = worker_template()
                tool_domains = {
                    "worker": {
                        volume_path(
                            volume,
                            TOOL_RUNTIME_DIRECTORY,
                            _WORKER_FILENAME,
                        ): worker,
                    },
                    "requirements": {
                        volume_path(
                            volume,
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
                    tools_directory=state_root / "tools",
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
                snapshot_result = DeploymentSnapshot(
                    workspace_id=workspace.id,
                    revision=snapshot.revision,
                    layout_version=DEPLOYMENT_SCHEMA_VERSION,
                    complete=False,
                    error=reason,
                    tool_runtime=_local_runtime_snapshot(runtime, reason),
                )
                return snapshot_result

            await commit_manifest(
                workspace.filesystem,
                volume,
                workspace_id=workspace.id,
                revision=snapshot.revision,
                realized=realized,
                previous=manifest,
            )
            snapshot_result = DeploymentSnapshot(
                workspace_id=workspace.id,
                revision=snapshot.revision,
                layout_version=DEPLOYMENT_SCHEMA_VERSION,
                complete=True,
                error=None,
                tool_runtime=_local_runtime_snapshot(runtime, None),
            )
            return snapshot_result

    async def _load_manifest(
        self,
        workspace: Workspace,
    ) -> _DeploymentManifest | None:
        """Read the completion manifest once per deployment instance."""

        if not self._manifest_loaded:
            self._manifest = await read_manifest(
                workspace.filesystem,
                volume_path(workspace.deployment_volume, DEPLOYMENT_MANIFEST),
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
                volume_path(
                    workspace.deployment_volume,
                    "tools",
                    "requirements.txt",
                ),
            )
        except _FilesystemError:
            return b""

    def _deployment_lock(self, workspace_id: str) -> asyncio.Lock:
        """Return the per-Workspace deployment lock for this event loop."""

        loop = asyncio.get_running_loop()
        locks = _DEPLOY_LOCKS.setdefault(loop, {})
        return locks.setdefault(workspace_id, asyncio.Lock())

    def _emit(
        self,
        kind: str,
        message: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        emit_event(
            self._events,
            RuntimeDiagnostic(kind=kind, message=message, detail=detail or {}),
        )


def _local_runtime_snapshot(
    runtime: _LocalToolRuntime | None,
    error: str | None,
) -> ToolRuntimeSnapshot:
    if runtime is None:
        return ToolRuntimeSnapshot(
            python=None,
            worker=None,
            tools_directory=None,
            binding_directory=None,
            error=error,
        )
    return ToolRuntimeSnapshot(
        python=str(runtime.python) if runtime.python is not None else None,
        worker=str(runtime.worker) if runtime.worker is not None else None,
        tools_directory=(
            str(runtime.tools_directory)
            if runtime.tools_directory is not None
            else None
        ),
        binding_directory=str(runtime.root),
        error=error or runtime.error,
    )
