"""Docker CapabilityDeployment: materialize snapshots into a Docker Workspace.

The Docker deployment connects the control plane to a Docker Workspace
(RFC-0017): it materializes a container-native Capability View (real
volume files; containers cannot follow Host symlinks), projects MCP stubs
and the invocation binding, publishes catalog index projections and the
Runtime-owned worker, keeps the private dependency environment in sync
through one dedicated, cleanable setup container, and records one
completion manifest binding the published artifacts to the snapshot
revision, the Workspace identity, and the volume mount contract.

All artifact writes address Backend-relative capability volume paths
through the Workspace filesystem; only the inherently container-native
mechanics (Repertoire materialization, venv creation and dependency
synchronization, MCP discovery) run in transient containers that are
always removed afterwards and never become implicit owners of later
executions. The ToolExecutor consumes only the reconciled deployment and
Workspace primitives, and its worker containers mount the persistent
volume without mapping any Host path into the container namespace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import shlex
import weakref
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cli_agent.runtime._backend import _WorkspaceFilesystem
from cli_agent.runtime._backend.docker.backend import _DockerBackendWorkspace
from cli_agent.runtime._backend.docker.execution import (
    _DockerToolExecution,
    _run_docker_setup,
)
from cli_agent.runtime._backend.facts import (
    _DirectoryEntry,
    _FileMetadata,
    _ToolExecutionRequest,
)
from cli_agent.runtime._backend.local.tool_runtime import (
    effective_requirements,
    worker_template,
)
from cli_agent.runtime._capability.deployment import (
    DEPLOYMENT_MANIFEST,
    DEPLOYMENT_SCHEMA_VERSION,
    TOOL_RUNTIME_DIRECTORY,
    DeploymentSnapshot,
    StaleDeploymentError,
    ToolExecutor,
    _DeploymentManifest,
    commit_manifest,
    publish_artifacts,
    publish_domains,
    read_manifest,
    validate_tool_bindings,
    verify_deployment,
    volume_path,
)
from cli_agent.runtime._capability.facts import (
    _CapabilityInspection,
    _FilesystemError,
    _Provenance,
)
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    _MCPServerFacts,
    _MCPToolFacts,
)
from cli_agent.runtime._capability.mcp.stubs import materialize_stubs
from cli_agent.runtime._capability.projections import render_catalog_indexes
from cli_agent.runtime._capability.source import _CAPABILITY_DIRECTORIES
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime._environment.handlers.base import _CommandContext
from cli_agent.runtime._environment.handlers.executions import _text_execution
from cli_agent.runtime._execution import (
    BackendExecutionError,
    ExecutionHandle,
)
from cli_agent.runtime._project_instructions import (
    _ProjectInstructions,
    validate_instructions,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

if TYPE_CHECKING:
    from cli_agent.runtime._capability.provider import CapabilitySnapshot
    from cli_agent.runtime._workspace import Workspace

_WORKER_FILENAME = "worker.py"
_EFFECTIVE_REQUIREMENTS = "effective-requirements.txt"
_REQUIREMENTS_DIGEST = "requirements.sha256"
_VIEW_DIRECTORY = ".capability-view"
_LOWER_DIRECTORY = "lower"
_WHITEOUT_DIRECTORY = "whiteouts"
_DISCOVERY_RETRIES = 3
_AGENTS_MD_FILENAME = "AGENTS.md"
_DEPLOY_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[Path, asyncio.Lock],
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class _DockerToolRuntime:
    """One ensured Docker Tool Runtime or a fail-soft unavailable state.

    Every path is container-native inside the capability volume; the
    executor only passes them to worker containers.
    """

    python: str | None
    worker: str | None
    tools_directory: str | None
    binding_directory: str | None
    error: str | None

    @property
    def available(self) -> bool:
        return (
            self.python is not None
            and self.worker is not None
            and self.tools_directory is not None
            and self.binding_directory is not None
            and self.error is None
        )


class _DockerCapabilityView:
    """Volume-native Bound Capability View over one Docker Workspace.

    The Repertoire lower tree is materialized into the capability volume as
    real files and kept in sync on every attach by content comparison
    against lower copies under ``.capability-view/lower``: repertoire
    updates propagate to untouched upper files, Workspace overrides and
    whiteout markers are preserved, and stale lower files are removed
    without touching Workspace-owned upper files. Workspace mutations are
    direct volume writes, so no copy-up seam exists.
    """

    def __init__(
        self,
        backend: _DockerBackendWorkspace,
        volume: str,
    ) -> None:
        self.root = volume
        self._backend = backend
        self._volume = volume

    @classmethod
    async def materialize(
        cls,
        backend: _DockerBackendWorkspace,
        volume: str,
        repertoire: Path,
    ) -> _DockerCapabilityView:
        """Materialize one Docker Bound Capability View from the Repertoire."""

        view = cls(backend, volume)
        await view._sync(repertoire)
        return view

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        """Return trusted provenance and shadow facts for one view path."""

        relative = _managed_capability_path(relative_path)
        filesystem = self._backend.filesystem
        upper_meta = await _try_stat(filesystem, self._upper_path(relative))
        lower_meta = await _try_stat(filesystem, self._lower_path(relative))
        if await self._has_whiteout(relative):
            provenance: _Provenance | None = "whiteout"
        elif upper_meta is None:
            provenance = "repertoire" if lower_meta is not None else None
        elif lower_meta is None:
            provenance = "workspace"
        elif upper_meta.kind == "directory" and lower_meta.kind == "directory":
            provenance = "repertoire"
        elif upper_meta.kind == "directory" or lower_meta.kind == "directory":
            provenance = "workspace"
        else:
            upper = await filesystem.read(self._upper_path(relative))
            lower = await filesystem.read(self._lower_path(relative))
            provenance = "repertoire" if upper == lower else "workspace"

        valid = True
        validation_error = None
        if (
            provenance == "workspace"
            and lower_meta is not None
            and upper_meta is not None
            and (upper_meta.kind == "directory") != (lower_meta.kind == "directory")
        ):
            valid = False
            validation_error = (
                "Workspace override type does not match the Repertoire path"
            )

        return _CapabilityInspection(
            relative_path=relative,
            provenance=provenance,
            shadows_repertoire=(provenance == "workspace" and lower_meta is not None),
            valid=valid,
            validation_error=validation_error,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        """Return sorted effective entries for one managed directory."""

        relative = _managed_capability_path(relative_path)
        filesystem = self._backend.filesystem
        try:
            entries = await filesystem.list(self._upper_path(relative))
        except _FilesystemError:
            return ()
        visible = []
        for entry in entries:
            if entry.metadata.kind == "directory":
                visible.append(entry)
                continue
            if await self._has_whiteout(posixpath.join(relative, entry.name)):
                continue
            visible.append(entry)
        return tuple(visible)

    async def read(self, relative_path: str) -> bytes:
        """Read one managed file from the effective view."""

        relative = _managed_capability_path(relative_path)
        return await self._backend.filesystem.read(self._upper_path(relative))

    async def stat(self, relative_path: str) -> _FileMetadata:
        """Return effective metadata for one managed path."""

        relative = _managed_capability_path(relative_path)
        return await self._backend.filesystem.stat(self._upper_path(relative))

    async def _sync(self, repertoire: Path) -> None:
        filesystem = self._backend.filesystem
        repertoire_files = _repertoire_files(repertoire)
        for relative, content in repertoire_files.items():
            if await self._has_whiteout(relative):
                continue
            upper_path = self._upper_path(relative)
            lower_path = self._lower_path(relative)
            upper = await _try_read(filesystem, upper_path)
            if upper is not None:
                old_lower = await _try_read(filesystem, lower_path)
                if old_lower is None or old_lower != upper:
                    continue
                if upper == content:
                    continue
            await publish_artifacts(
                filesystem,
                {upper_path: content, lower_path: content},
            )
        for relative in await self._materialized_relatives():
            if relative in repertoire_files and not await self._has_whiteout(
                relative,
            ):
                continue
            upper = await _try_read(filesystem, self._upper_path(relative))
            lower = await _try_read(filesystem, self._lower_path(relative))
            if upper is not None and lower is not None and upper == lower:
                with suppress(_FilesystemError):
                    await filesystem.remove(self._upper_path(relative))
            with suppress(_FilesystemError):
                await filesystem.remove(self._lower_path(relative))

    async def _materialized_relatives(self) -> frozenset[str]:
        lower_root = self._lower_root()
        found: set[str] = set()
        for path in await _walk_files(self._backend.filesystem, lower_root):
            found.add(path.removeprefix(lower_root.rstrip("/") + "/"))
        return frozenset(found)

    async def _has_whiteout(self, relative: str) -> bool:
        return (
            await _try_stat(self._backend.filesystem, self._whiteout_path(relative))
            is not None
        )

    def _upper_path(self, relative: str) -> str:
        return volume_path(self._volume, relative)

    def _lower_root(self) -> str:
        return volume_path(self._volume, _VIEW_DIRECTORY, _LOWER_DIRECTORY)

    def _lower_path(self, relative: str) -> str:
        return volume_path(self._lower_root(), relative)

    def _whiteout_path(self, relative: str) -> str:
        return volume_path(self._volume, _VIEW_DIRECTORY, _WHITEOUT_DIRECTORY, relative)


class _DockerToolExecutor:
    """ToolExecutor over the Docker Tool Runtime in one Workspace.

    The worker, its venv, and the effective Tools directory come from the
    Tool Runtime the deployment attached to the Backend Workspace; every
    path is container-native inside the persistent volume. A stale,
    foreign, or incomplete deployment, an invalid binding, or a missing or
    failed Tool Runtime fails this execution with text output before any
    worker starts; worker containers never mount Host paths.
    """

    def __init__(
        self,
        backend: _DockerBackendWorkspace,
        *,
        workspace_id: str,
        revision: str,
        deployment: DeploymentSnapshot,
        runtime: _DockerToolRuntime | None,
    ) -> None:
        self._backend = backend
        self._workspace_id = workspace_id
        self._revision = revision
        self._deployment = deployment
        self._runtime = runtime

    def prepare(
        self,
        request: _ToolExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        """Prepare one Tool worker execution without starting a container.

        Args:
            request (`_ToolExecutionRequest`):
                The Tool code and its logical bindings.
            context (`_CommandContext`):
                The Session execution facts (cwd and environment).

        Returns:
            One worker ``ExecutionHandle``; the worker container is
            created only when the handle runs.

        Raises:
            RuntimeError: If the Backend Workspace is closed.
        """

        self._backend._ensure_open()
        deployment = self._deployment
        if not deployment.complete:
            return _text_execution(
                (deployment.error or "Tool environment is unavailable") + "\n",
                success=False,
            )
        try:
            verify_deployment(
                deployment,
                revision=self._revision,
                workspace_id=self._workspace_id,
            )
        except StaleDeploymentError as exc:
            return _text_execution(
                f"Tool environment is unavailable: {exc}\n",
                success=False,
            )
        invalid_binding = validate_tool_bindings(request.bindings)
        if invalid_binding is not None:
            return _text_execution(
                f"Tool environment is unavailable: {invalid_binding}\n",
                success=False,
            )
        runtime = self._runtime
        if (
            runtime is None
            or not runtime.available
            or runtime.python is None
            or runtime.worker is None
            or runtime.tools_directory is None
            or runtime.binding_directory is None
        ):
            return _text_execution(
                (
                    runtime.error
                    if runtime is not None and runtime.error is not None
                    else "Tool environment is unavailable"
                )
                + "\n",
                success=False,
            )

        cwd = _resolve_path(self._backend.root, context.cwd, ".")
        payload = json.dumps(
            {
                "code": request.code,
                "workspace": self._backend.root,
                "cwd": cwd,
                "tools_directory": runtime.tools_directory,
                "binding_directory": runtime.binding_directory,
                "tool_paths": {
                    binding.name: binding.path for binding in request.bindings
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        python = runtime.python
        environment = {
            **self._backend.execution_base_environment(),
            **context.environment,
        }
        environment["VIRTUAL_ENV"] = posixpath.dirname(posixpath.dirname(python))
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        bin_directory = posixpath.dirname(python)
        existing_path = environment.get("PATH")
        environment["PATH"] = (
            bin_directory
            if not existing_path
            else bin_directory + os.pathsep + existing_path
        )
        return _DockerToolExecution(
            self._backend,
            python=python,
            worker=runtime.worker,
            payload=payload,
            cwd=cwd,
            environment=environment,
        )


class _DockerCapabilityDeployment:
    """Docker deployment between CapabilitySnapshots and a Workspace."""

    def __init__(
        self,
        *,
        state_root: Path,
        repertoire: Path,
        volume: str,
        backend: _DockerBackendWorkspace,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> None:
        self._state_root = state_root
        self._repertoire = repertoire
        self._volume = volume
        self._backend = backend
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
        """Return the ToolExecutor bound to the latest reconciled deployment."""

        docker_backend = workspace.backend
        if not isinstance(docker_backend, _DockerBackendWorkspace):
            raise ValueError(
                "Docker ToolExecutor requires a Docker Backend Workspace",
            )
        deployment = self._deployment or DeploymentSnapshot(
            workspace_id=workspace.id,
            revision=revision,
            layout_version=DEPLOYMENT_SCHEMA_VERSION,
            complete=False,
            error="Tool environment is unavailable",
        )
        return _DockerToolExecutor(
            docker_backend,
            workspace_id=workspace.id,
            revision=revision,
            deployment=deployment,
            runtime=docker_backend._tool_runtime,
        )

    async def attach(
        self,
        workspace: Workspace,
    ) -> _LogicalCapabilityView:
        """Materialize the Docker Bound Capability View (idempotent)."""

        docker_backend = workspace.backend
        if not isinstance(docker_backend, _DockerBackendWorkspace):
            raise ValueError(
                "Docker deployment requires a Docker Backend Workspace",
            )
        return await _DockerCapabilityView.materialize(
            docker_backend,
            self._volume,
            self._repertoire,
        )

    async def discover_mcp(
        self,
        configs: tuple[MCPServerConfig, ...],
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> tuple[_MCPServerFacts, ...]:
        """Discover Workspace MCP servers from the Docker execution context.

        One transient setup container installs the MCP client and contacts
        every configured server with the container execution environment;
        the container is removed afterwards, so discovery leaves nothing
        behind. Exhaustion of one server's attempts emits a diagnostic and
        produces no facts for that server.
        """

        if not configs:
            return ()
        environment = dict(self._backend.execution_base_environment())
        payload = json.dumps(
            {
                "servers": [config.to_dict() for config in configs],
                "retries": _DISCOVERY_RETRIES,
            },
        ).encode("utf-8")
        try:
            effective = effective_requirements(b"", include_mcp=True)
            await publish_artifacts(
                self._backend.filesystem,
                {
                    volume_path(
                        self._volume,
                        TOOL_RUNTIME_DIRECTORY,
                        _EFFECTIVE_REQUIREMENTS,
                    ): effective,
                },
            )
            runtime = await _ensure_docker_tool_runtime(
                self._backend,
                volume=self._volume,
                effective=effective,
            )
            if not runtime.available or runtime.python is None:
                raise BackendExecutionError(
                    runtime.error or "Docker MCP discovery runtime is unavailable",
                )
            bin_directory = posixpath.dirname(runtime.python)
            environment["VIRTUAL_ENV"] = posixpath.dirname(bin_directory)
            environment["PATH"] = bin_directory + os.pathsep + os.defpath
            environment["PYTHONNOUSERSITE"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            command = f"{_quote(runtime.python)} -c {shlex.quote(_DISCOVERY_SCRIPT)}"
            exit_code, output = await _run_docker_setup(
                self._backend,
                command=command,
                environment=environment,
                input_data=payload,
            )
        except BackendExecutionError as exc:
            for config in configs:
                self._emit(
                    "mcp.discovery_failed",
                    f"MCP server {config.name} discovery failed after "
                    f"{_DISCOVERY_RETRIES} attempts",
                    {"server": config.name, "error": str(exc)},
                )
            return ()
        parsed = _parse_discovery_output(output) if exit_code == 0 else None
        if parsed is None:
            for config in configs:
                self._emit(
                    "mcp.discovery_failed",
                    f"MCP server {config.name} discovery failed after "
                    f"{_DISCOVERY_RETRIES} attempts",
                    {"server": config.name, "error": _tail(output)},
                )
            return ()
        facts: list[_MCPServerFacts] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            tools = entry.get("tools")
            if not isinstance(tools, list):
                self._emit(
                    "mcp.discovery_failed",
                    f"MCP server {name} discovery failed after "
                    f"{_DISCOVERY_RETRIES} attempts",
                    {"server": name, "error": str(entry.get("error", ""))},
                )
                continue
            facts.append(
                _MCPServerFacts(
                    name=name,
                    tools=tuple(
                        _MCPToolFacts(
                            name=tool["name"],
                            description=str(tool.get("description", "")),
                            input_schema=tool.get("input_schema", {}),
                        )
                        for tool in tools
                        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                    ),
                ),
            )
        return tuple(facts)

    async def materialize_stubs(
        self,
        workspace: Workspace,
        configs: tuple[MCPServerConfig, ...],
        facts: tuple[_MCPServerFacts, ...],
    ) -> None:
        """Project generated MCP stubs and the invocation binding.

        Unchanged stub and binding domains are skipped via the completion
        manifest; a binding materialization failure keeps the previous
        deployment in place.
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
        sync by its own digest marker inside one dedicated, cleanable
        setup container; a failure keeps the previous complete deployment
        on disk and returns an incomplete DeploymentSnapshot instead of
        raising, and the completion manifest is only rewritten after every
        domain materialized.
        """

        docker_backend = workspace.backend
        if not isinstance(docker_backend, _DockerBackendWorkspace):
            raise ValueError(
                "Docker deployment requires a Docker Backend Workspace",
            )
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
                requirements = await self._read_requirements(workspace)
                effective = effective_requirements(
                    requirements,
                    include_mcp=bool(snapshot.mcp_servers),
                )
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
                runtime = await _ensure_docker_tool_runtime(
                    docker_backend,
                    volume=self._volume,
                    effective=effective,
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
                    docker_backend._attach_tool_runtime(runtime)
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
            docker_backend._attach_tool_runtime(runtime)
            snapshot_result = DeploymentSnapshot(
                workspace_id=workspace.id,
                revision=snapshot.revision,
                layout_version=DEPLOYMENT_SCHEMA_VERSION,
                complete=True,
                error=None,
                mounts=(docker_backend.volume,),
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


async def _docker_project_instructions(
    backend: _DockerBackendWorkspace,
) -> _ProjectInstructions | None:
    """Load and validate the Workspace-root ``AGENTS.md`` from the volume."""

    try:
        content = await backend.filesystem.read(_AGENTS_MD_FILENAME)
    except _FilesystemError:
        return None
    return validate_instructions(
        source=f"{backend.root}/{_AGENTS_MD_FILENAME}",
        content=content,
    )


async def _ensure_docker_tool_runtime(
    backend: _DockerBackendWorkspace,
    *,
    volume: str,
    effective: bytes,
) -> _DockerToolRuntime:
    """Ensure the venv and digest-gated dependencies inside the volume.

    The setup container creates the private venv, installs the effective
    requirements, and writes the digest marker only after success; a
    failed or interrupted setup leaves no marker, so the next reconcile
    retries. The setup container is removed on every path and never owns
    the materialized environment.
    """

    tool_directory = volume_path(volume, TOOL_RUNTIME_DIRECTORY)
    absolute_tool_directory = posixpath.join(backend.root, tool_directory)
    venv_directory = posixpath.join(absolute_tool_directory, ".venv")
    python = posixpath.join(venv_directory, "bin", "python")
    worker = posixpath.join(absolute_tool_directory, _WORKER_FILENAME)
    tools_directory = posixpath.join(backend.root, volume_path(volume, "tools"))
    digest = hashlib.sha256(effective).hexdigest()
    marker_path = posixpath.join(absolute_tool_directory, _REQUIREMENTS_DIGEST)
    filesystem = backend.filesystem
    venv_ready = await _try_stat(filesystem, python) is not None
    marker = await _try_read(filesystem, marker_path)
    marker_matches = (
        marker is not None and marker.decode("ascii", errors="replace").strip() == digest
    )
    if not venv_ready or not marker_matches:
        requirements_path = posixpath.join(
            absolute_tool_directory,
            _EFFECTIVE_REQUIREMENTS,
        )
        create = f"python3 -m venv --without-pip {_quote(venv_directory)}"
        install = ""
        if effective.strip():
            install = (
                f" && {_quote(python)} -m ensurepip --upgrade"
                f" && {_quote(python)} -m pip install --quiet --disable-pip-version-check"
                f" --retries 0 --timeout 5 -r {_quote(requirements_path)}"
            )
        command = (
            f"{create}{install}"
            f" && printf '%s' {_quote(digest)} > {_quote(marker_path)}"
        )
        exit_code, output = await _run_docker_setup(
            backend,
            command=command,
            environment=dict(backend.execution_base_environment()),
        )
        if exit_code != 0:
            return _DockerToolRuntime(
                python=None,
                worker=None,
                tools_directory=None,
                binding_directory=None,
                error=f"Tool environment is unavailable: {_tail(output)}",
            )
    return _DockerToolRuntime(
        python=python,
        worker=worker,
        tools_directory=tools_directory,
        binding_directory=absolute_tool_directory,
        error=None,
    )


def _repertoire_files(repertoire: Path) -> dict[str, bytes]:
    """Return every Repertoire capability file by relative path and content."""

    files: dict[str, bytes] = {}
    for directory in _CAPABILITY_DIRECTORIES:
        root = repertoire / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(
                    f"Repertoire capability paths must not be symbolic links: {path}",
                )
            if not path.is_file():
                continue
            files[path.relative_to(repertoire).as_posix()] = path.read_bytes()
    return files


async def _walk_files(filesystem: _WorkspaceFilesystem, directory: str) -> set[str]:
    """Return every file path under one filesystem directory, recursively."""

    try:
        entries = await filesystem.list(directory)
    except _FilesystemError:
        return set()
    found: set[str] = set()
    for entry in entries:
        relative = posixpath.join(directory, entry.name)
        if entry.metadata.kind == "directory":
            found |= await _walk_files(filesystem, relative)
        else:
            found.add(relative)
    return found


async def _try_read(filesystem: _WorkspaceFilesystem, path: str) -> bytes | None:
    try:
        return await filesystem.read(path)
    except _FilesystemError:
        return None


async def _try_stat(
    filesystem: _WorkspaceFilesystem, path: str
) -> _FileMetadata | None:
    try:
        return await filesystem.stat(path)
    except _FilesystemError:
        return None


def _managed_capability_path(path: str) -> str:
    relative = posixpath.normpath(path)
    if (
        posixpath.isabs(relative)
        or not relative
        or relative.split("/")[0] not in _CAPABILITY_DIRECTORIES
        or relative in {".", ".."}
        or ".." in relative.split("/")
    ):
        raise ValueError("capability path must be managed and relative")
    return relative


def _resolve_path(root: str, cwd: str, path: str) -> str:
    candidate = path if posixpath.isabs(path) else posixpath.join(cwd, path)
    return posixpath.normpath(candidate)


def _parse_discovery_output(output: str) -> list[dict[str, object]] | None:
    """Parse the last JSON array line of one discovery run's output."""

    for line in reversed(output.splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def _quote(path: str) -> str:
    return shlex.quote(path)


def _tail(output: str) -> str:
    detail = output.strip()
    if len(detail) > 4_000:
        detail = detail[-4_000:]
    return detail or "setup execution failed"


_DISCOVERY_SCRIPT = r"""
import asyncio
import json
import os
import sys

from mcp import ClientSession


def main():
    payload = json.load(sys.stdin)
    json.dump(asyncio.run(discover_all(payload)), sys.stdout)
    sys.stdout.write("\n")


async def discover_all(payload):
    results = []
    for server in payload["servers"]:
        result = {"name": server["name"]}
        try:
            result["tools"] = await discover(server, int(payload.get("retries", 1)))
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


async def discover(server, retries):
    last_error = None
    for attempt in range(retries):
        try:
            return await list_tools_once(server)
        except Exception as exc:
            last_error = exc
    raise last_error


async def list_tools_once(server):
    if server["transport"] == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = server["command"]
        env = {
            name: os.environ[name]
            for name in server["env"]
            if name in os.environ
        } or None
        params = StdioServerParameters(
            command=command[0],
            args=list(command[1:]),
            env=env,
        )
        async with stdio_client(params) as streams:
            return await collect(streams)

    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    headers = {
        header: os.environ[key]
        for header, key in server["headers"]
        if key in os.environ
    }
    async with httpx2.AsyncClient(headers=headers or None) as http_client:
        async with streamable_http_client(
            server["url"],
            http_client=http_client,
        ) as streams:
            return await collect(streams)


async def collect(streams):
    read, write = streams
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema or {},
            }
            for tool in result.tools
        ]


if __name__ == "__main__":
    main()
"""
