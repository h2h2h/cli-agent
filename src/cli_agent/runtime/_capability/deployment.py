"""CapabilityDeployment: the deployment plane between control plane and Workspace.

The control plane (``CapabilityProvider``) discovers immutable logical
``CapabilitySnapshot`` facts without side effects; this module defines the
only materialization boundary between those snapshots and a Workspace.
Deploying means attaching the Capability View, projecting MCP stubs,
materializing the Tool worker, its private environment and dependencies,
and the invocation binding, then recording one completion manifest that
binds the published artifacts to the snapshot revision and the Workspace
identity.

Backend-neutral capability volume layout (issue RFC-0014), rooted at the
Workspace state volume and addressed with Backend-relative paths so any
Backend can host it and per-execution environments can mount it read-only:

    <capability volume>/
    ├── tools/  skills/  library/  _mcp/   # Capability View upper tree
    ├── .capability-view/whiteouts/        # View mutation markers
    ├── .tool-environment/                 # Tool runtime volume
    │   ├── .venv/                         # Private environment
    │   ├── worker.py                      # Runtime-owned worker
    │   ├── mcp_binding.py                 # MCP invocation binding
    │   ├── effective-requirements.txt     # Combined dependencies
    │   └── requirements.sha256            # Dependency digest marker
    └── deployment.json                    # Deployment completion manifest

Deployment artifacts live under the persistent capability volume and never
depend on the lifecycle of any single ExecutionHandle.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from cli_agent.runtime._backend import (
    _FileWriteRequest,
    _ToolBinding,
    _ToolExecutionRequest,
    _WorkspaceFilesystem,
)
from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.snapshot import CapabilitySnapshot
from cli_agent.runtime._environment.handlers.base import _CommandContext
from cli_agent.runtime._execution import ExecutionHandle

if TYPE_CHECKING:
    from cli_agent.runtime._workspace import Workspace

DEPLOYMENT_SCHEMA_VERSION = 1

TOOL_RUNTIME_DIRECTORY = ".tool-environment"
DEPLOYMENT_MANIFEST = "deployment.json"


@dataclass(frozen=True, slots=True)
class DeploymentSnapshot:
    """One capability deployment bound to a snapshot and a Workspace.

    ``revision`` is the fingerprint of the exact ``CapabilitySnapshot``
    handed to ``reconcile``; ``complete`` is False when any deployed
    runtime component (most commonly the private dependency environment)
    failed to materialize, in which case ``error`` carries the reason and
    the previous complete deployment on disk remains in place. ``mounts``
    records the Backend-native volume mount contract of the deployed
    capability runtime (RFC-0017); the Local deployment owns the Workspace
    state volume and records no extra mount.
    """

    workspace_id: str
    revision: str
    layout_version: int
    complete: bool
    error: str | None
    mounts: tuple[str, ...] = ()
    tool_runtime: ToolRuntimeSnapshot | None = None


@dataclass(frozen=True, slots=True)
class ToolRuntimeSnapshot:
    """Backend-neutral logical paths for one deployed Tool runtime."""

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


@runtime_checkable
class ToolExecutor(Protocol):
    """Convert one deployed Tool request into one ``ExecutionHandle``.

    An executor is composed by the Runtime with the active Workspace
    identity, the active Capability snapshot revision, and the reconciled
    ``DeploymentSnapshot``. ``prepare`` is synchronous and free of external
    side effects: it validates the deployment before any side effect and
    returns a handle whose worker or transport starts only in ``run``.
    """

    def prepare(
        self,
        request: _ToolExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        """Prepare one Tool execution without starting work or resources."""
        ...


@runtime_checkable
class ToolExecutorFactory(Protocol):
    """Create a ToolExecutor from immutable deployment facts."""

    def create(
        self,
        workspace: Workspace,
        snapshot: CapabilitySnapshot,
        deployment: DeploymentSnapshot,
    ) -> ToolExecutor:
        """Bind one executor to the Workspace and snapshot revision."""
        ...


@runtime_checkable
class CapabilityDeployment(Protocol):
    """Materialize one CapabilitySnapshot into a Workspace."""

    async def reconcile(
        self,
        snapshot: CapabilitySnapshot,
        workspace: Workspace,
    ) -> DeploymentSnapshot:
        """Deploy the snapshot idempotently and return the deployment facts.

        Reconciling the same snapshot into the same Workspace must not
        reinstall anything: unchanged artifact domains are skipped via the
        completion manifest, and a failed reconcile never marks an
        incomplete deployment complete.
        """
        ...


class StaleDeploymentError(RuntimeError):
    """Raised when a deployment does not match what execution requested."""

    def __init__(
        self,
        reason: str,
        *,
        workspace_id: str,
        revision: str,
    ) -> None:
        super().__init__(reason)
        self.workspace_id = workspace_id
        self.revision = revision


def verify_deployment(
    deployment: DeploymentSnapshot,
    *,
    revision: str,
    workspace_id: str,
) -> None:
    """Reject stale, incomplete, or foreign deployments before execution.

    Args:
        deployment (`DeploymentSnapshot`):
            The deployment an executor was composed with.
        revision (`str`):
            The snapshot revision the execution is being prepared for.
        workspace_id (`str`):
            The Workspace identity the execution runs in.

    Raises:
        StaleDeploymentError: If the deployment belongs to another
            Workspace, was deployed from a different snapshot revision,
            or did not complete.
    """

    if deployment.workspace_id != workspace_id:
        raise StaleDeploymentError(
            "Deployment belongs to a different Workspace",
            workspace_id=deployment.workspace_id,
            revision=deployment.revision,
        )
    if deployment.revision != revision:
        raise StaleDeploymentError(
            "Deployment is stale for the active capability revision",
            workspace_id=deployment.workspace_id,
            revision=deployment.revision,
        )
    if not deployment.complete:
        raise StaleDeploymentError(
            "Deployment did not complete",
            workspace_id=deployment.workspace_id,
            revision=deployment.revision,
        )


def validate_tool_bindings(
    bindings: tuple[_ToolBinding, ...],
) -> str | None:
    """Return the first invalid binding, or None when every binding is safe.

    Bindings are the requested capabilities of one Tool run: names must be
    non-keyword Python identifiers and paths must stay inside the logical
    Tools tree, so a worker can never be steered outside the materialized
    Tools directory. Shared by every ToolExecutor regardless of Backend.
    """

    for binding in bindings:
        name = binding.name
        path = binding.path
        if (
            not isinstance(name, str)
            or not name
            or not name.isidentifier()
            or keyword.iskeyword(name)
        ):
            return f"invalid Tool binding: {name!r}"
        if (
            not isinstance(path, str)
            or posixpath.isabs(path)
            or not path.startswith("tools/")
            or ".." in posixpath.normpath(path).split("/")
        ):
            return f"invalid Tool path: {path!r}"
    return None


@dataclass(frozen=True, slots=True)
class _DeploymentManifest:
    """Completion manifest binding published artifacts to their inputs."""

    workspace_id: str
    revision: str
    complete: bool
    digests: Mapping[str, str]

    @classmethod
    def decode(cls, content: bytes) -> _DeploymentManifest | None:
        """Parse one manifest, treating any unreadable content as absent."""

        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping):
            return None
        workspace_id = raw.get("workspace_id")
        revision = raw.get("revision")
        complete = raw.get("complete")
        digests = raw.get("digests")
        if (
            not isinstance(workspace_id, str)
            or not isinstance(revision, str)
            or not isinstance(complete, bool)
            or not isinstance(digests, Mapping)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in digests.items()
            )
        ):
            return None
        return cls(
            workspace_id=workspace_id,
            revision=revision,
            complete=complete,
            digests=dict(digests),
        )

    def encode(self) -> bytes:
        """Render the canonical manifest bytes."""

        return (
            json.dumps(
                {
                    "schema_version": DEPLOYMENT_SCHEMA_VERSION,
                    "workspace_id": self.workspace_id,
                    "revision": self.revision,
                    "complete": self.complete,
                    "digests": dict(sorted(self.digests.items())),
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


def artifact_digest(artifacts: Mapping[str, bytes]) -> str:
    """Fingerprint one artifact domain by path and content."""

    hasher = hashlib.sha256()
    for path in sorted(artifacts):
        content = artifacts[path]
        name = path.encode("utf-8")
        hasher.update(len(name).to_bytes(8, "big"))
        hasher.update(name)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()


async def read_manifest(
    filesystem: _WorkspaceFilesystem,
    manifest_path: str,
) -> _DeploymentManifest | None:
    """Read the completion manifest; missing or corrupt manifests are absent."""

    try:
        content = await filesystem.read(manifest_path)
    except _FilesystemError:
        return None
    return _DeploymentManifest.decode(content)


async def write_manifest(
    filesystem: _WorkspaceFilesystem,
    manifest_path: str,
    manifest: _DeploymentManifest,
) -> None:
    """Atomically publish the completion manifest."""

    await filesystem.write(
        _FileWriteRequest(path=manifest_path, content=manifest.encode()),
    )


async def publish_artifacts(
    filesystem: _WorkspaceFilesystem,
    artifacts: Mapping[str, bytes],
) -> None:
    """Atomically publish every artifact file through the Workspace."""

    for path in sorted(artifacts):
        await filesystem.write(
            _FileWriteRequest(path=path, content=artifacts[path]),
        )


async def publish_domains(
    *,
    filesystem: _WorkspaceFilesystem,
    workspace_id: str,
    manifest: _DeploymentManifest | None,
    realized: Mapping[str, str],
    domains: Mapping[str, Mapping[str, bytes]],
) -> dict[str, str]:
    """Publish every changed artifact domain and return the realized digests.

    Each domain (e.g. ``indexes``, ``worker``, ``requirements``, ``stubs``,
    ``binding``) is skipped when the completion manifest already covers its
    digest for this Workspace; unchanged domains are never republished.
    """

    updated = dict(realized)
    for domain, artifacts in domains.items():
        desired = {domain: artifact_digest(artifacts)}
        if not domains_match(
            manifest,
            workspace_id=workspace_id,
            digests=desired,
        ):
            await publish_artifacts(filesystem, artifacts)
            updated.update(desired)
    return updated


async def commit_manifest(
    filesystem: _WorkspaceFilesystem,
    volume: str,
    *,
    workspace_id: str,
    revision: str,
    realized: Mapping[str, str],
    previous: _DeploymentManifest | None,
) -> None:
    """Atomically publish the completion manifest when anything changed.

    A complete manifest binds the published artifact digests to the
    snapshot revision and the Workspace identity; failed reconciles never
    reach this point, so the previous complete deployment stays in place.
    """

    published = _DeploymentManifest(
        workspace_id=workspace_id,
        revision=revision,
        complete=True,
        digests=dict(realized),
    )
    if previous != published:
        await write_manifest(
            filesystem,
            volume_path(volume, DEPLOYMENT_MANIFEST),
            published,
        )


def domains_match(
    manifest: _DeploymentManifest | None,
    *,
    workspace_id: str,
    digests: Mapping[str, str],
) -> bool:
    """Return whether the manifest already covers the requested domains."""

    if manifest is None or not manifest.complete:
        return False
    if manifest.workspace_id != workspace_id:
        return False
    return all(manifest.digests.get(key) == value for key, value in digests.items())


def volume_path(volume: str, *parts: str) -> str:
    """Join one Backend-relative path inside the capability volume."""

    return posixpath.join(volume, *parts)
