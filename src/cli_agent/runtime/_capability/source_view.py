"""Host-side logical Capability View over source trees.

The control plane reads capability source directly: Workspace upper files,
Repertoire lower files, and whiteout markers, without materializing
symlinks, starting processes, or writing anything. Every read is recorded
so a snapshot revision can fingerprint exactly what discovery consumed.
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path
from typing import Protocol, runtime_checkable

from cli_agent.runtime._capability.facts import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileMetadata,
    _filesystem_error,
    _Provenance,
)
from cli_agent.runtime._capability.source import _CAPABILITY_DIRECTORIES

_DEFAULT_MODE = 0o644


@runtime_checkable
class _LogicalCapabilityView(Protocol):
    """Read-only effective capability facts for one source tree.

    The protocol is satisfied by both the Host source view used by the
    CapabilityProvider and the materialized Bound Capability View owned by
    a Backend; neither may write through this interface.
    """

    root: str

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        """Return provenance and shadow facts for one managed path."""
        ...

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        """Return sorted effective entries for one managed directory."""
        ...

    async def read(self, relative_path: str) -> bytes:
        """Read one managed file from the effective source."""
        ...

    async def stat(self, relative_path: str) -> _FileMetadata:
        """Return effective metadata for one managed path."""
        ...


class _CapabilitySourceView:
    """Effective capability facts computed from Host source, never written.

    Upper entries shadow or merge with lower entries exactly like the
    materialized Bound View: real upper files are Workspace provenance,
    upper symbolic links are validated lower links, whiteout markers hide
    lower entries, and directories merge across layers.
    """

    def __init__(self, *, upper_root: Path, repertoire: Path) -> None:
        self.root = str(upper_root)
        self._upper = upper_root
        self._repertoire = repertoire
        self._whiteouts = upper_root / ".capability-view" / "whiteouts"
        self._reads: list[tuple[str, bytes]] = []

    @property
    def fingerprint_inputs(self) -> tuple[tuple[str, bytes], ...]:
        """Return every ``(relative, content)`` pair read since creation."""

        return tuple(sorted(self._reads, key=lambda item: item[0]))

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        relative = _managed_path(relative_path)
        upper = self._upper / relative
        lower = self._repertoire / relative
        whiteout = self._whiteout_path(relative)

        if upper.is_symlink():
            if not _is_exact_lower_link(upper, lower):
                raise ValueError(
                    f"invalid Workspace capability symbolic link: {relative}"
                )
            provenance: _Provenance | None = "repertoire"
        elif _lexists(upper):
            if upper.is_dir():
                provenance = "repertoire" if lower.is_dir() else "workspace"
            else:
                provenance = "workspace"
        elif whiteout.is_file():
            provenance = "whiteout"
        elif _lexists(lower):
            provenance = "repertoire"
        else:
            provenance = None

        valid = True
        validation_error = None
        if (
            provenance == "workspace"
            and _lexists(lower)
            and upper.is_dir() != lower.is_dir()
        ):
            valid = False
            validation_error = (
                "Workspace override type does not match the Repertoire path"
            )

        return _CapabilityInspection(
            relative_path=relative.as_posix(),
            provenance=provenance,
            shadows_repertoire=(provenance == "workspace" and _lexists(lower)),
            valid=valid,
            validation_error=validation_error,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        relative = _managed_path(relative_path)
        directory = self._upper / relative
        try:
            with os.scandir(directory) as scanned:
                entries = tuple(scanned)
        except FileNotFoundError:
            entries = ()
        except OSError as exc:
            raise _filesystem_error(relative_path, exc) from exc
        upper_names = {entry.name for entry in entries}
        listed = tuple(
            _DirectoryEntry(
                name=entry.name,
                metadata=_metadata(os.stat(self._upper / relative / entry.name)),
            )
            for entry in entries
        )

        lower = self._repertoire / relative
        if lower.is_dir():
            try:
                with os.scandir(lower) as entries:
                    for entry in entries:
                        if entry.name in upper_names:
                            continue
                        if self._whiteout_path(relative / entry.name).is_file():
                            continue
                        listed += (
                            _DirectoryEntry(
                                name=entry.name,
                                metadata=_metadata(
                                    os.stat(self._repertoire / relative / entry.name)
                                ),
                            ),
                        )
            except OSError as exc:
                raise _filesystem_error(relative_path, exc) from exc

        return tuple(sorted(listed, key=lambda entry: entry.name))

    async def read(self, relative_path: str) -> bytes:
        relative = _managed_path(relative_path)
        target = self._resolve_target(relative)
        if target is None:
            raise _filesystem_error(
                str(relative),
                FileNotFoundError(str(relative)),
            )
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise _filesystem_error(str(relative), exc) from exc
        self._reads.append((relative.as_posix(), content))
        return content

    async def stat(self, relative_path: str) -> _FileMetadata:
        relative = _managed_path(relative_path)
        target = self._resolve_target(relative)
        if target is None:
            raise _filesystem_error(
                str(relative),
                FileNotFoundError(str(relative)),
            )
        try:
            return _metadata(os.stat(target))
        except OSError as exc:
            raise _filesystem_error(str(relative), exc) from exc

    def _resolve_target(self, relative: Path) -> Path | None:
        upper = self._upper / relative
        if upper.is_symlink():
            lower = self._repertoire / relative
            if not _is_exact_lower_link(upper, lower):
                raise ValueError(
                    f"invalid Workspace capability symbolic link: {relative}"
                )
            return lower
        if _lexists(upper):
            return upper
        lower = self._repertoire / relative
        if self._whiteout_path(relative).is_file():
            return None
        if _lexists(lower):
            return lower
        return None

    def _whiteout_path(self, relative: Path) -> Path:
        return self._whiteouts / relative


class _RecordingCapabilityView:
    """Delegate one logical view and record every read for fingerprinting."""

    def __init__(self, inner: _LogicalCapabilityView) -> None:
        self._inner = inner
        self._reads: list[tuple[str, bytes]] = []

    @property
    def root(self) -> str:
        return self._inner.root

    @property
    def fingerprint_inputs(self) -> tuple[tuple[str, bytes], ...]:
        """Return every ``(relative, content)`` pair read since creation."""

        return tuple(sorted(self._reads, key=lambda item: item[0]))

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        return await self._inner.inspect(relative_path)

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        return await self._inner.list(relative_path)

    async def read(self, relative_path: str) -> bytes:
        content = await self._inner.read(relative_path)
        self._reads.append((relative_path, content))
        return content

    async def stat(self, relative_path: str) -> _FileMetadata:
        return await self._inner.stat(relative_path)


def _managed_path(path: str) -> Path:
    relative = Path(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] not in _CAPABILITY_DIRECTORIES
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("capability path must be managed and relative")
    return relative


def _is_exact_lower_link(view_path: Path, lower_path: Path) -> bool:
    if not view_path.is_symlink():
        return False
    try:
        target = Path(os.readlink(view_path))
    except OSError:
        return False
    if not target.is_absolute():
        target = view_path.parent / target
    return Path(os.path.abspath(os.path.normpath(str(target)))) == Path(
        os.path.abspath(os.path.normpath(str(lower_path)))
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _metadata(stat_result: os.stat_result) -> _FileMetadata:
    if stat_module.S_ISREG(stat_result.st_mode):
        kind = "file"
    elif stat_module.S_ISDIR(stat_result.st_mode):
        kind = "directory"
    elif stat_module.S_ISLNK(stat_result.st_mode):
        kind = "symlink"
    else:
        kind = "other"
    return _FileMetadata(
        kind=kind,
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        mode=stat_result.st_mode,
    )
