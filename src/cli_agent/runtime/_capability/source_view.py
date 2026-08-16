"""Host-side logical Capability View over source trees.

The control plane reads capability source directly: Workspace upper files,
Repertoire lower files, and whiteout markers, without materializing
symlinks, starting processes, or writing anything. Every read is recorded
so a snapshot revision can fingerprint exactly what discovery consumed.
"""

from __future__ import annotations

import os
import posixpath
import stat as stat_module
from pathlib import Path
from typing import Protocol, runtime_checkable

from cli_agent.runtime._capability.facts import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileMetadata,
    _filesystem_error,
    _FilesystemError,
    _Provenance,
)
from cli_agent.runtime._capability.source import _CAPABILITY_DIRECTORIES

_DEFAULT_MODE = 0o644


@runtime_checkable
class CapabilitySource(Protocol):
    """Read-only effective capability facts for one source tree.

    The protocol is satisfied by both the Host source view used by the
    CapabilityProvider and the materialized Bound Capability View owned by
    a Backend; neither may write through this interface.
    """

    @property
    def root(self) -> str:
        """Return the Backend-native root represented by this view."""
        ...

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


class CapabilitySourceFactory(Protocol):
    """Create one read-only logical source for an opened Workspace."""

    async def create(self, workspace: _SourceWorkspace) -> CapabilitySource:
        """Return a source without materializing deployment artifacts."""
        ...


class _SourceFilesystem(Protocol):
    async def stat(self, path: str) -> _FileMetadata: ...

    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]: ...

    async def read(self, path: str) -> bytes: ...


class _SourceWorkspace(Protocol):
    @property
    def root(self) -> str: ...

    @property
    def filesystem(self) -> _SourceFilesystem: ...

    @property
    def repertoire(self) -> Path: ...

    @property
    def deployment_volume(self) -> str: ...


class _HostCapabilitySource:
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


class _HostCapabilitySourceFactory:
    """Create the Local Host source over Workspace upper and Repertoire lower."""

    async def create(self, workspace: _SourceWorkspace) -> CapabilitySource:
        return _HostCapabilitySource(
            upper_root=Path(workspace.root) / workspace.deployment_volume,
            repertoire=workspace.repertoire,
        )


class _WorkspaceCapabilitySourceFactory:
    """Create a source combining Backend upper facts with a Host lower tree."""

    async def create(self, workspace: _SourceWorkspace) -> CapabilitySource:
        return _WorkspaceCapabilitySource(
            workspace=workspace,
            volume=workspace.deployment_volume,
            repertoire=workspace.repertoire,
        )


class _WorkspaceCapabilitySource:
    """Read-only logical source for non-Host Workspace filesystems.

    Workspace upper entries are read through ``Workspace.filesystem`` while
    Repertoire lower entries remain Host-owned inputs. Discovery never copies
    lower files into the Workspace and never writes whiteouts or projections.
    """

    def __init__(
        self,
        *,
        workspace: _SourceWorkspace,
        volume: str,
        repertoire: Path,
    ) -> None:
        self.root = volume
        self._workspace = workspace
        self._volume = volume
        self._repertoire = repertoire

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        relative = _managed_path(relative_path)
        relative_text = relative.as_posix()
        upper = await self._upper_stat(relative_text)
        lower_path = self._repertoire / relative
        lower = _try_host_metadata(lower_path)
        whiteout = await self._has_whiteout(relative_text)

        if whiteout:
            provenance: _Provenance | None = "whiteout"
        elif upper is None:
            provenance = "repertoire" if lower is not None else None
        elif lower is None:
            provenance = "workspace"
        elif upper.kind == "directory" and lower.kind == "directory":
            provenance = "repertoire"
        elif upper.kind == "directory" or lower.kind == "directory":
            provenance = "workspace"
        else:
            upper_content = await self._workspace.filesystem.read(
                self._upper_path(relative_text),
            )
            lower_content = lower_path.read_bytes()
            provenance = (
                "repertoire" if upper_content == lower_content else "workspace"
            )

        valid = True
        validation_error = None
        if (
            provenance == "workspace"
            and upper is not None
            and lower is not None
            and (upper.kind == "directory") != (lower.kind == "directory")
        ):
            valid = False
            validation_error = (
                "Workspace override type does not match the Repertoire path"
            )
        return _CapabilityInspection(
            relative_path=relative_text,
            provenance=provenance,
            shadows_repertoire=(provenance == "workspace" and lower is not None),
            valid=valid,
            validation_error=validation_error,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        relative = _managed_path(relative_path)
        relative_text = relative.as_posix()
        try:
            upper_entries = await self._workspace.filesystem.list(
                self._upper_path(relative_text),
            )
        except _FilesystemError:
            upper_entries = ()
        entries = {entry.name: entry for entry in upper_entries}

        lower = self._repertoire / relative
        if lower.is_dir():
            try:
                with os.scandir(lower) as scanned:
                    for entry in scanned:
                        if entry.name in entries:
                            continue
                        child = posixpath.join(relative_text, entry.name)
                        if await self._has_whiteout(child):
                            continue
                        entries[entry.name] = _DirectoryEntry(
                            name=entry.name,
                            metadata=_metadata(entry.stat(follow_symlinks=False)),
                        )
            except OSError as exc:
                raise _filesystem_error(relative_text, exc) from exc
        return tuple(sorted(entries.values(), key=lambda entry: entry.name))

    async def read(self, relative_path: str) -> bytes:
        relative = _managed_path(relative_path)
        relative_text = relative.as_posix()
        if await self._has_whiteout(relative_text):
            raise _filesystem_error(
                relative_text,
                FileNotFoundError(relative_text),
            )
        if await self._upper_stat(relative_text) is not None:
            return await self._workspace.filesystem.read(
                self._upper_path(relative_text),
            )
        try:
            return (self._repertoire / relative).read_bytes()
        except OSError as exc:
            raise _filesystem_error(relative_text, exc) from exc

    async def stat(self, relative_path: str) -> _FileMetadata:
        relative = _managed_path(relative_path)
        relative_text = relative.as_posix()
        if await self._has_whiteout(relative_text):
            raise _filesystem_error(
                relative_text,
                FileNotFoundError(relative_text),
            )
        upper = await self._upper_stat(relative_text)
        if upper is not None:
            return upper
        try:
            return _metadata(os.stat(self._repertoire / relative))
        except OSError as exc:
            raise _filesystem_error(relative_text, exc) from exc

    async def _upper_stat(self, relative: str) -> _FileMetadata | None:
        try:
            return await self._workspace.filesystem.stat(self._upper_path(relative))
        except _FilesystemError:
            return None

    async def _has_whiteout(self, relative: str) -> bool:
        try:
            await self._workspace.filesystem.stat(self._whiteout_path(relative))
        except _FilesystemError:
            return False
        return True

    def _upper_path(self, relative: str) -> str:
        return posixpath.join(self._volume, relative)

    def _whiteout_path(self, relative: str) -> str:
        return posixpath.join(
            self._volume,
            ".capability-view",
            "whiteouts",
            relative,
        )


class _RecordingCapabilitySource:
    """Delegate one logical view and record every read for fingerprinting."""

    def __init__(self, inner: CapabilitySource) -> None:
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


def _try_host_metadata(path: Path) -> _FileMetadata | None:
    try:
        return _metadata(os.stat(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _filesystem_error(str(path), exc) from exc


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
