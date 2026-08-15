"""Thin logical Workspace with stable identity and a stable Backend binding.

A Workspace answers "where is the Agent currently working": it owns a
stable logical identity, an agent-visible root, filesystem access, and
one Backend binding that stays fixed for the Workspace lifetime (V1 has
no transparent hot-swap). The Backend answers "how does I/O execute
here"; capability materialization belongs to the CapabilityDeployment
plane, which the Local Workspace feeds from its persisted Host facts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _WorkspaceFilesystem,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.docker import (
    _DockerBackend,
    _DockerWorkspaceSource,
)
from cli_agent.runtime._backend.local import _LocalBackend
from cli_agent.runtime._capability.source import _prepare_capability_source
from cli_agent.runtime._capability.workspace import (
    _load_workspace_env,
    _prepare_workspace,
)

_IDENTITY_PATTERN = re.compile(r"local:[0-9a-f]{32}")
_DOCKER_IDENTITY_PATTERN = re.compile(r"docker:[0-9a-f]{32}")
_DOCKER_VOLUME_PREFIX = "cli-agent-docker-"
_DEFAULT_DOCKER_IMAGE = "python:3.12-alpine"


class Workspace(Protocol):
    """One thin logical working environment for an active Runtime."""

    id: str
    root: str
    filesystem: _WorkspaceFilesystem
    backend: _BackendWorkspace

    async def close(self) -> None:
        """Flush and close the bound Backend; later use fails closed."""
        ...


class WorkspaceFactory(Protocol):
    """Open Workspaces from Host-side locations."""

    async def open(
        self,
        workspace: str | Path,
        *,
        repertoire: str | Path | None,
    ) -> Workspace:
        """Open one Workspace; any open failure must fail closed."""
        ...


class _LocalWorkspaceFactory:
    """Open Local Workspaces over Host project directories.

    Each open generates or reads the stable identity persisted beside
    the project, opens one Local Backend for it, and exposes the
    persisted Host facts (state root, Repertoire, capability volume)
    the Local CapabilityDeployment consumes.
    """

    async def open(
        self,
        workspace: str | Path,
        *,
        repertoire: str | Path | None,
    ) -> _LocalWorkspace:
        """Open one Local Workspace for the given project directory.

        Args:
            workspace (`str | Path`):
                Existing directory to bind as the Workspace root.
            repertoire (`str | Path | None`):
                User-maintained capability lower tree.

        Returns:
            The opened `_LocalWorkspace`.

        Raises:
            ValueError: If the directory is missing, the environment or
                identity files are invalid, or the Backend cannot open.
        """

        paths = _prepare_workspace(workspace)
        workspace_id = _load_workspace_identity(paths.state)
        repertoire_root = _prepare_capability_source(repertoire, paths.state)
        backend = await _LocalBackend().open_workspace(
            source=_WorkspaceSource(root=paths.root, environment=paths.environment),
        )
        return _LocalWorkspace(workspace_id, paths.root, backend, repertoire_root)


class _LocalWorkspace:
    """One Local Workspace binding a stable identity to a Host directory."""

    def __init__(
        self,
        workspace_id: str,
        root: Path,
        backend: _BackendWorkspace,
        repertoire: Path,
    ) -> None:
        self.id = workspace_id
        self.root = str(root)
        self.root_path = root
        self.filesystem = backend.filesystem
        self.backend = backend
        self.repertoire = repertoire

    @property
    def state_root(self) -> Path:
        """Return the persisted Workspace state directory (``.workspace``)."""

        return self.root_path / ".workspace"

    @property
    def deployment_volume(self) -> str:
        """Return the Backend-relative capability volume path."""

        return self.state_root.relative_to(self.root_path).as_posix()

    async def close(self) -> None:
        """Close the bound Backend idempotently.

        Flushing pending Workspace changes before close is the
        Runtime's close choreography, not the Workspace's.
        """

        await self.backend.close()


class _DockerWorkspaceFactory:
    """Open Docker Workspaces over one persistent volume per identity.

    The Host control directory stores the same Workspace state facts as a
    Local Workspace (environment file, Repertoire) plus a separate Docker
    identity, while all actual data lives in one persistent Docker volume
    named from that identity. Sessions therefore bind to a stable logical
    identity and never rebind between the Local and the Docker
    environment of the same project directory.
    """

    def __init__(self, *, image: str = _DEFAULT_DOCKER_IMAGE) -> None:
        self._image = image

    async def open(
        self,
        workspace: str | Path,
        *,
        repertoire: str | Path | None,
    ) -> _DockerWorkspace:
        """Open one Docker Workspace for the given control directory.

        Args:
            workspace (`str | Path`):
                Existing directory holding the Workspace control facts
                (identity, environment file, Repertoire).
            repertoire (`str | Path | None`):
                User-maintained capability lower tree.

        Returns:
            The opened `_DockerWorkspace` over the persistent volume.

        Raises:
            ValueError: If the control directory is invalid, the identity
                cannot be established, or the Docker Backend cannot open.
        """

        paths = _prepare_workspace(workspace)
        workspace_id = _load_docker_workspace_identity(paths.state)
        repertoire_root = _prepare_capability_source(repertoire, paths.state)
        environment = _load_workspace_env(paths.environment)
        backend = await _DockerBackend().open_workspace(
            _DockerWorkspaceSource(
                volume=_docker_volume_name(workspace_id),
                image=self._image,
                root="/workspace",
                environment=environment,
            )
        )
        return _DockerWorkspace(workspace_id, backend, repertoire_root)


class _DockerWorkspace:
    """One Docker Workspace binding a stable identity to a persistent volume."""

    def __init__(
        self,
        workspace_id: str,
        backend: _BackendWorkspace,
        repertoire: Path,
    ) -> None:
        self.id = workspace_id
        self.root = backend.root
        self.filesystem = backend.filesystem
        self.backend = backend
        self.repertoire = repertoire

    @property
    def volume(self) -> str:
        """Return the persistent Docker volume backing this Workspace."""
        return _docker_volume_name(self.id)

    async def close(self) -> None:
        """Close the bound Backend idempotently; the volume stays durable."""

        await self.backend.close()


def _load_docker_workspace_identity(state: Path) -> str:
    """Return the stable Docker identity, generating it on first open.

    The identity persists beside the project directory in a separate
    ``identity.docker`` file, so the same directory can never silently
    rebind a Session between the Local and the Docker environment.

    Args:
        state (`Path`): The Workspace state directory (``.workspace``).

    Returns:
        The stable identity string, e.g. ``docker:<32 hex chars>``.

    Raises:
        ValueError: If the identity file cannot be created, read, or
            validated.
    """

    identity_path = state / "identity.docker"
    identity = f"docker:{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(identity_path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(f"cannot create workspace identity: {identity_path}") from exc
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(identity)
        return identity
    try:
        content = identity_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read workspace identity: {identity_path}") from exc
    if _DOCKER_IDENTITY_PATTERN.fullmatch(content) is None:
        raise ValueError(f"invalid workspace identity: {identity_path}")
    return content


def _docker_volume_name(workspace_id: str) -> str:
    """Return the persistent volume name for one Docker workspace identity."""
    return f"{_DOCKER_VOLUME_PREFIX}{workspace_id.removeprefix('docker:')}"


def _load_workspace_identity(state: Path) -> str:
    """Return the stable workspace identity, generating it on first open.

    The identity persists beside the project directory, so it stays
    equal across processes and survives directory moves; a corrupted or
    unexpected identity file fails closed instead of silently minting a
    new identity for an existing project.

    Args:
        state (`Path`): The Workspace state directory (``.workspace``).

    Returns:
        The stable identity string, e.g. ``local:<32 hex chars>``.

    Raises:
        ValueError: If the identity file cannot be created, read, or
            validated.
    """

    identity_path = state / "identity"
    identity = f"local:{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(identity_path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(f"cannot create workspace identity: {identity_path}") from exc
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(identity)
        return identity
    try:
        content = identity_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read workspace identity: {identity_path}") from exc
    if _IDENTITY_PATTERN.fullmatch(content) is None:
        raise ValueError(f"invalid workspace identity: {identity_path}")
    return content
