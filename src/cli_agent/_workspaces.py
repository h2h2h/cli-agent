"""Host-side Local and Docker Workspace implementations.

These concrete factories bind Host-selected configuration to one stable
Backend.  The Runtime consumes only the neutral ``Workspace`` and
``WorkspaceFactory`` ports from ``cli_agent.runtime._workspace``; concrete
selection belongs to the outer presets module.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from cli_agent.runtime._backend import (
    _FilesystemError,
    _ShellExecutionRequest,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.docker import (
    _DockerBackend,
    _DockerBackendWorkspace,
    _DockerWorkspaceSource,
)
from cli_agent.runtime._backend.local import _LocalBackend, _LocalBackendWorkspace
from cli_agent.runtime._capability.source import _prepare_capability_source
from cli_agent.runtime._capability.workspace import (
    _load_workspace_env,
    _prepare_workspace,
)
from cli_agent.runtime._execution import ExecutionHandle
from cli_agent.runtime._project_instructions import (
    _load_project_instructions,
    _ProjectInstructions,
    validate_instructions,
)

_IDENTITY_PATTERN = re.compile(r"local:[0-9a-f]{32}")
_DOCKER_IDENTITY_PATTERN = re.compile(r"docker:[0-9a-f]{32}")
_DOCKER_VOLUME_PREFIX = "cli-agent-docker-"
_DEFAULT_DOCKER_IMAGE = "python:3.12-alpine"


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
        backend: _LocalBackendWorkspace,
        repertoire: Path,
    ) -> None:
        self.id = workspace_id
        self.root = backend.root
        self.root_path = root
        self.filesystem = backend.filesystem
        self.backend = backend
        self.repertoire = repertoire

    @property
    def base_environment(self) -> Mapping[str, str]:
        """Return explicit Workspace configuration values."""

        return self.backend.workspace_environment

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> ExecutionHandle:
        """Prepare one Local shell execution through this Workspace."""

        return self.backend.prepare_shell(request)

    def execution_base_environment(self) -> Mapping[str, str]:
        return self.backend.execution_base_environment()

    async def load_project_instructions(self) -> _ProjectInstructions | None:
        return _load_project_instructions(self.root_path)

    async def flush(self) -> None:
        """Persist pending Local Workspace changes."""

        await self.backend.flush()

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
        return _DockerWorkspace(
            workspace_id,
            paths.state,
            backend,
            repertoire_root,
        )


class _DockerWorkspace:
    """One Docker Workspace binding a stable identity to a persistent volume."""

    def __init__(
        self,
        workspace_id: str,
        state_root: Path,
        backend: _DockerBackendWorkspace,
        repertoire: Path,
    ) -> None:
        self.id = workspace_id
        self.root = backend.root
        self.filesystem = backend.filesystem
        self.backend = backend
        self.repertoire = repertoire
        self._state_root = state_root

    @property
    def base_environment(self) -> Mapping[str, str]:
        """Return explicit Docker Workspace configuration values."""

        return self.backend.workspace_environment

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> ExecutionHandle:
        """Prepare one Docker shell execution through this Workspace."""

        return self.backend.prepare_shell(request)

    def execution_base_environment(self) -> Mapping[str, str]:
        return self.backend.execution_base_environment()

    async def load_project_instructions(self) -> _ProjectInstructions | None:
        try:
            content = await self.filesystem.read("AGENTS.md")
        except _FilesystemError:
            return None
        return validate_instructions(
            source=f"{self.root}/AGENTS.md",
            content=content,
        )

    async def flush(self) -> None:
        """Persist pending Docker Workspace changes."""

        await self.backend.flush()

    @property
    def state_root(self) -> Path:
        """Return the Host control state directory (``.workspace``)."""

        return self._state_root

    @property
    def deployment_volume(self) -> str:
        """Return the Backend-relative capability volume path."""

        return ".workspace"

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
