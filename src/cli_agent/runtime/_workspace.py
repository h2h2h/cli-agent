"""Thin logical Workspace with stable identity and a stable Backend binding.

A Workspace answers "where is the Agent currently working": it owns a
stable logical identity, an agent-visible root, filesystem access, and
one Backend binding that stays fixed for the Workspace lifetime (V1 has
no transparent hot-swap). The Backend answers "how does I/O execute
here"; the Runtime composes both without conflating them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _CapabilityState,
    _WorkspaceFilesystem,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.local import _LocalBackend
from cli_agent.runtime._capability.source import _prepare_capability_source
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime._capability.workspace import _prepare_workspace

_IDENTITY_PATTERN = re.compile(r"local:[0-9a-f]{32}")


class Workspace(Protocol):
    """One thin logical working environment for an active Runtime."""

    id: str
    root: str
    filesystem: _WorkspaceFilesystem
    backend: _BackendWorkspace
    capability_source: _LogicalCapabilityView

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
    the project, opens one Local Backend for it, and wraps both into a
    thin Local Workspace whose Backend binding never changes.
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
        capability_source = _prepare_capability_source(repertoire, paths.state)
        backend = await _LocalBackend().open_workspace(
            source=_WorkspaceSource(root=paths.root, environment=paths.environment),
            capability_source=capability_source,
            capability_state=_CapabilityState(root=paths.state),
        )
        return _LocalWorkspace(workspace_id, paths.root, backend)


class _LocalWorkspace:
    """One Local Workspace binding a stable identity to a Host directory."""

    def __init__(
        self,
        workspace_id: str,
        root: Path,
        backend: _BackendWorkspace,
    ) -> None:
        self.id = workspace_id
        self.root = str(root)
        self.root_path = root
        self.filesystem = backend.filesystem
        self.backend = backend
        self.capability_source = backend.capabilities

    async def close(self) -> None:
        """Close the bound Backend idempotently.

        Flushing pending Workspace changes before close is the
        Runtime's close choreography, not the Workspace's.
        """

        await self.backend.close()


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
