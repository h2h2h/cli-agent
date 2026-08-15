"""Backend-neutral capability facts shared by source views and catalogs.

These facts describe effective capability paths (provenance, shadowing,
metadata) and the read failures source views report; the Backend domain
reuses them for its Bound Capability View and Filesystem contracts.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass
from typing import Literal

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
class _FileMetadata:
    """Backend-neutral facts for one capability path."""

    kind: _FileKind
    size: int
    mtime_ns: int
    mode: int


@dataclass(frozen=True, slots=True)
class _DirectoryEntry:
    """One named entry returned by an effective capability listing."""

    name: str
    metadata: _FileMetadata


@dataclass(frozen=True, slots=True)
class _CapabilityInspection:
    """Trusted provenance and shadow facts for one managed capability path."""

    relative_path: str
    provenance: _Provenance | None
    shadows_repertoire: bool
    valid: bool
    validation_error: str | None


class _FilesystemError(Exception):
    """Backend-neutral failure raised by one capability source read."""

    def __init__(self, kind: _FilesystemErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _filesystem_error(path: str, exc: OSError) -> _FilesystemError:
    """Classify one Host filesystem error into a capability read failure."""

    error = exc.errno
    if error == errno.ENOENT:
        return _FilesystemError("not_found", f"No such file or directory: {path}")
    if error == errno.ENOTDIR:
        return _FilesystemError(
            "not_a_directory", f"path component is not a directory: {path}"
        )
    if error == errno.EISDIR:
        return _FilesystemError("is_directory", f"is a directory: {path}")
    if error == errno.EACCES:
        return _FilesystemError("permission_denied", f"permission denied: {path}")
    if error == errno.EEXIST:
        return _FilesystemError("already_exists", f"path already exists: {path}")
    return _FilesystemError("internal", f"filesystem error for {path}: {exc}")
