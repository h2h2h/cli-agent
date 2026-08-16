"""Local file-level Capability View materialized in the Workspace.

Implements the logical ``CapabilitySource`` contract (``root``,
``inspect``, ``list``, ``read``, ``stat``) with Host file mechanics: exact
lower symlinks, Workspace copy-up, persistent whiteouts, and the Shell
mutation lock. Materialization is owned by the CapabilityDeployment plane
(RFC-0014); ``prepare_path`` and ``prepare_shell`` are Local-only seams
consumed by the Local Filesystem and Local Shell execution; the generic
Backend contract never exposes them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

from cli_agent.runtime._backend.facts import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileMetadata,
    _Provenance,
)
from cli_agent.runtime._backend.local.filesystem import (
    _filesystem_error,
    _metadata,
)
from cli_agent.runtime._capability.command_parser import (
    FileRedirect,
    ShellParseResult,
    collect_redirects,
)
from cli_agent.runtime._capability.source import _CAPABILITY_DIRECTORIES
from cli_agent.runtime._capability.workspace import _ensure_real_directory


class _LocalCapabilityView:
    """Local file-level lower/upper Capability View materialized in Workspace."""

    def __init__(self, state_root: Path, repertoire: Path) -> None:
        self.root = str(state_root)
        self._root = state_root
        self._repertoire = repertoire
        self._whiteouts = state_root / ".capability-view" / "whiteouts"
        self._mutation_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def materialize(
        cls,
        state_root: Path,
        repertoire: Path,
    ) -> _LocalCapabilityView:
        """Create the Local layout and attach the effective Capability View.

        Args:
            state_root (`Path`):
                The Workspace state directory (``.workspace``).
            repertoire (`Path`):
                The Host Repertoire lower tree.

        Returns:
            The materialized Local Bound Capability View.

        Raises:
            ValueError: If the Repertoire root is missing or a Workspace
                capability symbolic link is invalid.
        """

        if not repertoire.is_dir():
            raise ValueError(f"repertoire must be an existing directory: {repertoire}")
        view = cls(state_root, repertoire)
        view._prepare_layout()
        view._attach()
        return view

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        """Return trusted provenance and shadow facts for one view path."""

        self._ensure_open()
        relative = _managed_capability_path(relative_path)
        view_path = self._resolve_managed(relative_path)
        lower_path = self._repertoire / relative
        whiteout = self._whiteout_path(relative)

        if view_path.is_symlink():
            if not _is_exact_lower_link(view_path, lower_path):
                raise ValueError(
                    f"invalid Workspace capability symbolic link: {relative}"
                )
            provenance: _Provenance | None = "repertoire"
        elif _lexists(view_path):
            if view_path.is_dir():
                provenance = "repertoire" if lower_path.is_dir() else "workspace"
            else:
                provenance = "workspace"
        elif whiteout.is_file():
            provenance = "whiteout"
        else:
            provenance = None

        valid = True
        validation_error = None
        if (
            provenance == "workspace"
            and _lexists(lower_path)
            and view_path.is_dir() != lower_path.is_dir()
        ):
            valid = False
            validation_error = (
                "Workspace override type does not match the Repertoire path"
            )

        return _CapabilityInspection(
            relative_path=relative.as_posix(),
            provenance=provenance,
            shadows_repertoire=(provenance == "workspace" and _lexists(lower_path)),
            valid=valid,
            validation_error=validation_error,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        """Return sorted effective entries for one managed directory."""

        self._ensure_open()
        relative = _managed_capability_path(relative_path)
        directory = self._resolve_managed(relative_path)
        try:
            with os.scandir(directory) as entries:
                listed = tuple(
                    _DirectoryEntry(
                        name=entry.name,
                        metadata=_metadata(
                            os.stat(
                                self._resolve_managed(
                                    (relative / entry.name).as_posix()
                                )
                            )
                        ),
                    )
                    for entry in entries
                )
        except OSError as exc:
            raise _filesystem_error(relative_path, exc) from exc
        return tuple(sorted(listed, key=lambda entry: entry.name))

    async def read(self, relative_path: str) -> bytes:
        """Read one managed file from the effective view."""

        self._ensure_open()
        target = self._resolve_managed(relative_path)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise _filesystem_error(relative_path, exc) from exc

    async def stat(self, relative_path: str) -> _FileMetadata:
        """Return effective metadata for one managed path."""

        self._ensure_open()
        target = self._resolve_managed(relative_path)
        try:
            return _metadata(os.stat(target))
        except OSError as exc:
            raise _filesystem_error(relative_path, exc) from exc

    def prepare_path(self, path: Path) -> None:
        """Prepare one managed view path for a direct file mutation.

        Args:
            path (`Path`):
                The absolute target path a Runtime command is about to write.

        Raises:
            ValueError: If the path traverses a symbolic-link intermediate
                directory or is an invalid lower link.
        """

        self._ensure_open()
        if not self._is_in_view(path):
            return
        self._reject_symlink_intermediates(path)
        if path.is_symlink():
            self._copy_up(path)
            return
        if not _lexists(path):
            self._remove_whiteout(path.relative_to(self._root))

    @asynccontextmanager
    async def prepare_shell(
        self,
        command: ShellParseResult,
        cwd: Path,
        *,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[bool]:
        """Copy up output-redirected targets before one Shell command runs."""

        self._ensure_open()
        if not _may_mutate(command):
            yield not cancelled()
            return

        async with self._mutation_lock:
            if cancelled():
                yield False
                return
            for path in self._write_paths(command, cwd):
                self._copy_up(path)
            yield True

    def close(self) -> None:
        """Close this materialized View idempotently."""

        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Backend Workspace is closed")

    def _resolve_managed(self, relative_path: str) -> Path:
        relative = _managed_capability_path(relative_path)
        target = self._root / relative
        self._reject_symlink_intermediates(target)
        if target.is_symlink():
            lower = self._repertoire / relative
            if not _is_exact_lower_link(target, lower) or not _is_regular_file(lower):
                raise ValueError(
                    f"invalid Workspace capability symbolic link: {relative}"
                )
        return target

    def _prepare_layout(self) -> None:
        _ensure_real_directory(self._root, label="workspace state path")
        _ensure_real_directory(
            self._root / ".capability-view",
            label="Capability View metadata path",
        )
        _ensure_real_directory(
            self._whiteouts,
            label="Capability View whiteout path",
        )
        for name in _CAPABILITY_DIRECTORIES:
            _ensure_real_directory(
                self._root / name,
                label=f"Workspace {name} capability path",
            )

    def _attach(self) -> None:
        for name in _CAPABILITY_DIRECTORIES:
            view_root = self._root / name
            lower_root = self._repertoire / name
            self._remove_stale_lower_links(view_root, lower_root)
            if lower_root.is_dir():
                self._attach_directory(view_root, lower_root, Path(name))

    def _remove_stale_lower_links(
        self,
        view_directory: Path,
        lower_directory: Path,
    ) -> None:
        for entry in tuple(view_directory.iterdir()):
            lower_entry = lower_directory / entry.name
            if entry.is_symlink():
                if not _is_exact_lower_link(entry, lower_entry):
                    raise ValueError(
                        "Workspace capability symbolic links must point to the "
                        f"matching Repertoire file: {entry}"
                    )
                if not lower_entry.is_file():
                    entry.unlink()
                continue
            if entry.is_dir():
                self._remove_stale_lower_links(entry, lower_entry)

    def _attach_directory(
        self,
        view_directory: Path,
        lower_directory: Path,
        relative_directory: Path,
    ) -> None:
        for lower_entry in sorted(
            lower_directory.iterdir(), key=lambda path: path.name
        ):
            mode = lower_entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(
                    f"Repertoire capability paths must not be symbolic links: "
                    f"{lower_entry}"
                )

            relative = relative_directory / lower_entry.name
            view_entry = view_directory / lower_entry.name
            if stat.S_ISDIR(mode):
                if _lexists(view_entry):
                    if view_entry.is_symlink():
                        raise ValueError(
                            "Workspace capability symbolic links must point to "
                            f"matching Repertoire files: {view_entry}"
                        )
                    if not view_entry.is_dir():
                        continue
                else:
                    try:
                        view_entry.mkdir()
                    except FileExistsError:
                        if not view_entry.is_dir():
                            continue
                self._attach_directory(view_entry, lower_entry, relative)
                continue

            if not stat.S_ISREG(mode):
                raise ValueError(
                    f"Repertoire capability paths must be regular files or "
                    f"directories: {lower_entry}"
                )

            whiteout = self._whiteout_path(relative)
            if whiteout.is_file():
                if _is_exact_lower_link(view_entry, lower_entry):
                    view_entry.unlink()
                elif _lexists(view_entry):
                    self._remove_whiteout(relative)
                continue

            if not _lexists(view_entry):
                try:
                    view_entry.symlink_to(lower_entry)
                except FileExistsError:
                    if not _is_exact_lower_link(view_entry, lower_entry):
                        raise ValueError(
                            "Workspace capability path changed while attaching: "
                            f"{view_entry}"
                        ) from None
            elif view_entry.is_symlink() and not _is_exact_lower_link(
                view_entry,
                lower_entry,
            ):
                raise ValueError(
                    "Workspace capability symbolic links must point to the "
                    f"matching Repertoire file: {view_entry}"
                )

    def _write_paths(
        self,
        command: ShellParseResult,
        cwd: Path,
    ) -> tuple[Path, ...]:
        targets = [
            redirect.target.value
            or redirect.target.quoted_content
            or redirect.target.text
            for redirect in collect_redirects(command.root)
            if isinstance(redirect, FileRedirect)
            and redirect.is_output
            and redirect.target is not None
        ]
        return self._normalize_targets(targets, cwd)

    def _normalize_targets(
        self,
        targets: list[str],
        cwd: Path,
    ) -> tuple[Path, ...]:
        normalized: list[Path] = []
        for target in targets:
            if not target or target == "-":
                continue
            if any(character in target for character in "*?[{~$`"):
                return tuple(self._root / name for name in _CAPABILITY_DIRECTORIES)
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            lexical = Path(os.path.abspath(os.path.normpath(str(candidate))))
            if self._is_in_view(lexical):
                self._reject_symlink_intermediates(lexical)
                normalized.append(lexical)
        return tuple(dict.fromkeys(normalized))

    def _copy_up(self, path: Path) -> None:
        if path.is_symlink():
            lower_path = self._lower_for_view_path(path)
            if lower_path is None or not _is_exact_lower_link(path, lower_path):
                raise ValueError(f"invalid Workspace capability symbolic link: {path}")
            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=".cli-agent-copy-up-",
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(lower_path, temporary)
                os.replace(temporary, path)
            finally:
                if _lexists(temporary):
                    temporary.unlink()
            self._remove_whiteout(path.relative_to(self._root))
            return

        if path.is_dir():
            for child in tuple(path.rglob("*")):
                if child.is_symlink():
                    self._copy_up(child)

    def _is_in_view(self, path: Path) -> bool:
        return any(
            _is_relative_to(path, self._root / name) for name in _CAPABILITY_DIRECTORIES
        )

    def _lower_for_view_path(self, path: Path) -> Path | None:
        if not self._is_in_view(path):
            return None
        return self._repertoire / path.relative_to(self._root)

    def _reject_symlink_intermediates(self, path: Path) -> None:
        if self._root.is_symlink():
            raise ValueError(
                f"Capability View root must not be a symbolic link: {self._root}"
            )
        for name in _CAPABILITY_DIRECTORIES:
            view_root = self._root / name
            if not _is_relative_to(path, view_root):
                continue
            if view_root.is_symlink():
                raise ValueError(
                    "Capability View paths must not traverse symbolic "
                    f"link directories: {view_root}"
                )
            current = view_root
            for part in path.relative_to(view_root).parts[:-1]:
                current /= part
                if current.is_symlink():
                    raise ValueError(
                        "Capability View paths must not traverse symbolic "
                        f"link directories: {current}"
                    )
                if not _lexists(current):
                    break
            return

    def _whiteout_path(self, relative: Path) -> Path:
        return self._whiteouts / relative

    def _remove_whiteout(self, relative: Path) -> None:
        marker = self._whiteout_path(relative)
        if marker.is_file():
            marker.unlink()
        parent = marker.parent
        while parent != self._whiteouts:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _managed_capability_path(path: str) -> Path:
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


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _may_mutate(command: ShellParseResult) -> bool:
    return command.contains_output_redirection
