"""Local Host-filesystem Workspace Filesystem implementation.

Paths are resolved against the Workspace root unless absolute; the Local
Backend stays permissive like the current CLI, so absolute paths may leave
the Workspace root. ``stat`` reports effective facts like POSIX ``stat(2)``,
following symlinks; ``list`` reports raw entry kinds. Managed Capability
paths are routed through the Local Capability View before any direct
mutation.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from cli_agent.runtime._backend.edit import _detect_line_ending, _split_bom, apply_edits
from cli_agent.runtime._backend.facts import (
    _DirectoryEntry,
    _FileEditRequest,
    _FileEditResult,
    _FileKind,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
)

if TYPE_CHECKING:
    from cli_agent.runtime._backend.local.view import _LocalCapabilityView


def _noop() -> None:
    """Provide an always-open lifecycle gate for standalone filesystem tests."""


class _LocalWorkspaceFilesystem:
    """Host-filesystem-backed Workspace Filesystem."""

    def __init__(
        self,
        root: Path,
        view_provider: Callable[[], _LocalCapabilityView | None] | None = None,
        ensure_open: Callable[[], None] | None = None,
    ) -> None:
        self._root = root
        self._view_provider = view_provider or (lambda: None)
        self._ensure_open = ensure_open or _noop

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        """Resolve one Local Backend path against a Session cwd without I/O."""

        self._ensure_open()
        target = _resolve_path(Path(cwd), path)
        return _ResolvedPath(
            path=str(target),
            within_workspace=_is_within(target, self._root),
        )

    async def stat(self, path: str) -> _FileMetadata:
        self._ensure_open()
        target = self._resolve(path)
        try:
            return _metadata(os.stat(target))
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc

    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]:
        self._ensure_open()
        target = self._resolve(path)
        try:
            with os.scandir(target) as entries:
                directory = tuple(
                    _DirectoryEntry(
                        name=entry.name,
                        metadata=_metadata(entry.stat(follow_symlinks=False)),
                    )
                    for entry in entries
                )
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc
        return tuple(sorted(directory, key=lambda entry: entry.name))

    async def read(self, path: str) -> bytes:
        self._ensure_open()
        target = self._resolve(path)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        self._ensure_open()
        target = self._prepare_target(request.path)
        try:
            if target.is_dir() and not target.is_symlink():
                raise _FilesystemError(
                    "is_directory", f"is a directory: {request.path}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, request.content)
        except OSError as exc:
            raise _filesystem_error(request.path, exc) from exc
        except ValueError as exc:
            raise _FilesystemError("invalid_path", str(exc)) from exc
        return _FileWriteResult(path=request.path, bytes_written=len(request.content))

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        self._ensure_open()
        target = self._prepare_target(request.path)
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise _filesystem_error(request.path, exc) from exc
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _FilesystemError(
                "invalid_content", "file is not valid UTF-8"
            ) from exc
        bom, text = _split_bom(text)
        line_ending = _detect_line_ending(text)
        try:
            updated = apply_edits(
                text.replace("\r\n", "\n"), request.edits, request.path
            )
        except ValueError as exc:
            raise _FilesystemError("edit_failed", str(exc)) from exc
        if line_ending == "\r\n":
            updated = updated.replace("\n", "\r\n")
        await self.write(
            _FileWriteRequest(
                path=request.path,
                content=(bom + updated).encode("utf-8"),
            )
        )
        return _FileEditResult(path=request.path, blocks_replaced=len(request.edits))

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        self._ensure_open()
        target = self._resolve(path)
        try:
            if target.is_dir() and not target.is_symlink():
                if not recursive:
                    raise _FilesystemError("is_directory", f"is a directory: {path}")
                shutil.rmtree(target)
            else:
                os.unlink(target)
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc

    def _resolve(self, path: str) -> Path:
        return _resolve_path(self._root, path)

    def _prepare_target(self, path: str) -> Path:
        target = self._resolve(path)
        view = self._view_provider()
        if view is not None:
            try:
                view.prepare_path(target)
            except ValueError as exc:
                raise _FilesystemError("invalid_path", str(exc)) from exc
        return target


def _resolve_path(root: Path, path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return Path(os.path.abspath(os.path.normpath(str(candidate))))


def _is_within(path: Path, directory: Path) -> bool:
    path_norm = os.path.normcase(os.path.normpath(str(path)))
    directory_norm = os.path.normcase(os.path.normpath(str(directory)))
    try:
        return os.path.commonpath([path_norm, directory_norm]) == directory_norm
    except ValueError:
        return False


def _metadata(info: os.stat_result) -> _FileMetadata:
    if stat.S_ISLNK(info.st_mode):
        kind: _FileKind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return _FileMetadata(
        kind=kind,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
    )


def _filesystem_error(path: str, exc: OSError) -> _FilesystemError:
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


def _atomic_write(path: Path, content: bytes) -> None:
    """Atomically replace one file, preserving its mode when present."""

    try:
        mode = stat.S_IMODE(path.stat().st_mode) if os.path.lexists(path) else None
    except OSError:
        mode = None
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".cli-agent-write-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), 0o644 if mode is None else mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            with suppress(OSError):
                temporary.unlink()
