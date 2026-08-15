"""Backend-neutral facts for Backend Workspace execution and filesystem.

Host-side ``Path`` values are confined to the ``open_workspace`` input fact
(``_WorkspaceSource``); every other fact carries opaque backend paths as
``str`` and never exposes Host ``Path``, file descriptors, stat objects, or
provider responses. Capability deployment facts live in the deployment
plane (``cli_agent.runtime._capability.deployment``) and MCP discovery
facts live with the capability plane (``cli_agent.runtime._capability.mcp``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._capability.facts import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileKind,
    _FileMetadata,
    _filesystem_error,
    _FilesystemError,
    _FilesystemErrorKind,
    _Provenance,
)

__all__ = (
    "_CapabilityInspection",
    "_DirectoryEntry",
    "_FileKind",
    "_FileMetadata",
    "_FilesystemError",
    "_FilesystemErrorKind",
    "_Provenance",
    "_filesystem_error",
)


@dataclass(frozen=True, slots=True)
class _WorkspaceSource:
    """Host-side Workspace input consumed by Backend open."""

    root: Path
    environment: Path


@dataclass(frozen=True, slots=True)
class _ShellExecutionRequest:
    """Backend-neutral facts for one ordinary Shell execution."""

    command: ShellParseResult
    cwd: str
    environment: Mapping[str, str]
    input_data: bytes | None = None


@dataclass(frozen=True, slots=True)
class _ToolBinding:
    """One logical Tool path visible inside the Backend Workspace."""

    name: str
    path: str


@dataclass(frozen=True, slots=True)
class _ToolExecutionRequest:
    """Backend-neutral facts for one Tool worker execution."""

    code: str
    cwd: str
    environment: Mapping[str, str]
    bindings: tuple[_ToolBinding, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedPath:
    """One Backend-native path resolved against a Session cwd."""

    path: str
    within_workspace: bool


@dataclass(frozen=True, slots=True)
class _FileWriteRequest:
    """One atomic Workspace write request."""

    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _FileWriteResult:
    """Backend-neutral outcome facts for one atomic Workspace write."""

    path: str
    bytes_written: int


@dataclass(frozen=True, slots=True)
class _FileEdit:
    """One exact-text replacement on a single Workspace file."""

    old_text: str
    new_text: str


@dataclass(frozen=True, slots=True)
class _FileEditRequest:
    """One atomic Workspace edit request."""

    path: str
    edits: tuple[_FileEdit, ...]


@dataclass(frozen=True, slots=True)
class _FileEditResult:
    """Backend-neutral outcome facts for one atomic Workspace edit."""

    path: str
    blocks_replaced: int
