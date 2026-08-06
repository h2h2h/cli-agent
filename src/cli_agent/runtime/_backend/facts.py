"""Backend-neutral facts for Backend Workspace execution and filesystem.

Host-side ``Path`` values are confined to the ``open_workspace`` input facts
(``_WorkspaceSource``, ``_CapabilitySource``, ``_CapabilityState``); every
other fact carries opaque backend paths as ``str`` and never exposes Host
``Path``, file descriptors, stat objects, or provider responses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cli_agent.runtime._capability.command_parser import ShellParseResult

_FileKind = Literal["file", "directory", "symlink", "other"]
_Provenance = Literal["repertoire", "workspace", "whiteout"]
_FilesystemErrorKind = Literal[
    "not_found",
    "not_a_directory",
    "is_directory",
    "permission_denied",
    "invalid_path",
    "invalid_content",
    "already_exists",
    "edit_failed",
    "unsupported",
    "internal",
]


@dataclass(frozen=True, slots=True)
class _WorkspaceSource:
    """Host-side Workspace input consumed by Backend open."""

    root: Path
    environment: Path


@dataclass(frozen=True, slots=True)
class _CapabilitySource:
    """Host-side Capability lower input consumed by Backend open."""

    repertoire: Path


@dataclass(frozen=True, slots=True)
class _CapabilityState:
    """Host-side persistent Capability state location consumed by Backend open."""

    root: Path


@dataclass(frozen=True, slots=True)
class _ShellExecutionRequest:
    """Backend-neutral facts for one ordinary Shell execution."""

    command: ShellParseResult
    cwd: str
    environment: Mapping[str, str]


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
class _FileMetadata:
    """Backend-neutral facts for one Workspace filesystem path."""

    kind: _FileKind
    size: int
    mtime_ns: int
    mode: int


@dataclass(frozen=True, slots=True)
class _ResolvedPath:
    """One Backend-native path resolved against a Session cwd."""

    path: str
    within_workspace: bool


@dataclass(frozen=True, slots=True)
class _DirectoryEntry:
    """One named entry returned by a Workspace directory listing."""

    name: str
    metadata: _FileMetadata


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


@dataclass(frozen=True, slots=True)
class _CapabilityInspection:
    """Trusted provenance and shadow facts for one managed capability path."""

    relative_path: str
    provenance: _Provenance | None
    shadows_repertoire: bool
    valid: bool
    validation_error: str | None


@dataclass(frozen=True, slots=True)
class _MCPToolFacts:
    """Provider-neutral facts for one discovered Workspace MCP tool."""

    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class _MCPServerFacts:
    """Provider-neutral facts for one discovered Workspace MCP server."""

    name: str
    tools: tuple[_MCPToolFacts, ...]


@dataclass(frozen=True, slots=True)
class _ToolRuntimeStatus:
    """Backend-neutral Tool Runtime availability facts."""

    available: bool
    error: str | None


class _FilesystemError(Exception):
    """Backend-neutral failure raised by one Workspace Filesystem operation."""

    def __init__(self, kind: _FilesystemErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
