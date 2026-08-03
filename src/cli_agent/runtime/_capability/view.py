"""Workspace Capability View backed by a user-maintained Repertoire."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

from cli_agent.runtime._capability.command_parser import (
    _DIRECT_MUTATORS,
    ShellParseResult,
    _sed_is_in_place,
    _strip_quotes,
    collect_redirects,
)
from cli_agent.runtime._capability.workspace import _ensure_real_directory

_CAPABILITY_DIRECTORIES = ("tools", "skills", "library", "_mcp")
_MCP_DIRECTORY = "_mcp"


@dataclass(frozen=True, slots=True)
class _CapabilityInspection:
    """Trusted layer facts for one managed Capability View path."""

    relative_path: str
    provenance: Literal["repertoire", "workspace", "whiteout"] | None
    shadows_repertoire: bool
    valid: bool
    validation_error: str | None


@dataclass(frozen=True, slots=True)
class _DeleteSnapshot:
    view_path: Path
    lower_path: Path
    lower_link: bool


class _CapabilityView:
    """Attach and maintain a file-level lower/upper capability view."""

    def __init__(self, workspace: Path, repertoire: Path) -> None:
        self.workspace = workspace
        self.root = workspace / ".workspace"
        self.repertoire = repertoire
        self._whiteouts = self.root / ".capability-view" / "whiteouts"
        self._mutation_lock = asyncio.Lock()

    @classmethod
    def open(
        cls,
        workspace: Path,
        repertoire: str | Path | None,
    ) -> _CapabilityView:
        """Create or validate both layers and attach the effective view."""

        repertoire_root = _prepare_repertoire(repertoire)
        if _paths_overlap(workspace / ".workspace", repertoire_root):
            raise ValueError("repertoire must be outside the Workspace state directory")

        view = cls(workspace, repertoire_root)
        view._prepare_layout()
        view._attach()
        return view

    @asynccontextmanager
    async def prepare_shell(
        self,
        command: ShellParseResult,
        cwd: Path,
        *,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[bool]:
        """Prepare recognized view mutations and reconcile deletion effects."""

        if not _may_mutate(command):
            yield not cancelled()
            return

        async with self._mutation_lock:
            if cancelled():
                deleted: tuple[_DeleteSnapshot, ...] = ()
                prepared = False
            else:
                delete_paths = self._delete_paths(command, cwd)
                deleted = self._snapshot_deletes(delete_paths)
                for path in self._write_paths(command, cwd):
                    self._copy_up(path)
                prepared = True
        try:
            yield prepared
        finally:
            if prepared:
                async with self._mutation_lock:
                    self._reconcile_deletes(deleted)

    def inspect(self, relative_path: str | Path) -> _CapabilityInspection:
        """Return trusted provenance and shadow facts for one view path."""

        relative = _managed_capability_path(relative_path)
        view_path = self.root / relative
        lower_path = self.repertoire / relative
        whiteout = self._whiteout_path(relative)

        if view_path.is_symlink():
            if not _is_exact_lower_link(view_path, lower_path):
                raise ValueError(
                    f"invalid Workspace capability symbolic link: {relative}"
                )
            provenance: Literal["repertoire", "workspace", "whiteout"] | None = (
                "repertoire"
            )
        elif _lexists(view_path):
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

    def _prepare_layout(self) -> None:
        _ensure_real_directory(self.root, label="workspace state path")
        _ensure_real_directory(
            self.root / ".capability-view",
            label="Capability View metadata path",
        )
        _ensure_real_directory(
            self._whiteouts,
            label="Capability View whiteout path",
        )
        for name in _CAPABILITY_DIRECTORIES:
            _ensure_real_directory(
                self.root / name,
                label=f"Workspace {name} capability path",
            )

    def _attach(self) -> None:
        for name in _CAPABILITY_DIRECTORIES:
            view_root = self.root / name
            lower_root = self.repertoire / name
            self._remove_stale_lower_links(view_root, lower_root)
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
            _strip_quotes(redirect.target.text)
            for redirect in collect_redirects(command.root)
            if redirect.is_output and redirect.target is not None
        ]
        executable = command.executable_basename
        operands = _operands(command.tokens[1:])

        if executable in {"chmod", "chown", "touch", "truncate", "tee"}:
            targets.extend(operands)
        elif executable in {"cp", "install", "ln"} and operands:
            targets.append(operands[-1])
        elif executable == "mv" and operands:
            targets.extend(operands)
        elif executable == "dd":
            targets.extend(
                token.split("=", 1)[1]
                for token in command.tokens[1:]
                if token.startswith("of=") and len(token) > 3
            )
        elif executable == "sed" and _sed_is_in_place(command.tokens[1:]):
            targets.extend(operands)
        elif executable == "patch":
            return tuple(self.root / name for name in _CAPABILITY_DIRECTORIES)

        return self._normalize_targets(targets, cwd)

    def _delete_paths(
        self,
        command: ShellParseResult,
        cwd: Path,
    ) -> tuple[Path, ...]:
        executable = command.executable_basename
        operands = _operands(command.tokens[1:])
        if executable in {"rm", "rmdir", "unlink"}:
            return self._normalize_targets(operands, cwd)
        if executable == "mv" and len(operands) >= 2:
            return self._normalize_targets(operands[:-1], cwd)
        return ()

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
                return tuple(self.root / name for name in _CAPABILITY_DIRECTORIES)
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
            self._remove_whiteout(path.relative_to(self.root))
            return

        if path.is_dir():
            for child in tuple(path.rglob("*")):
                if child.is_symlink():
                    self._copy_up(child)

    def _snapshot_deletes(
        self,
        targets: tuple[Path, ...],
    ) -> tuple[_DeleteSnapshot, ...]:
        snapshots: list[_DeleteSnapshot] = []
        for target in targets:
            candidates = (
                tuple(target.rglob("*"))
                if target.is_dir() and not target.is_symlink()
                else (target,)
            )
            for candidate in candidates:
                lower = self._lower_for_view_path(candidate)
                if lower is None or not lower.is_file():
                    continue
                if _is_exact_lower_link(candidate, lower):
                    snapshots.append(_DeleteSnapshot(candidate, lower, lower_link=True))
                elif _lexists(candidate) and not candidate.is_dir():
                    snapshots.append(
                        _DeleteSnapshot(candidate, lower, lower_link=False)
                    )
        return tuple(snapshots)

    def _reconcile_deletes(
        self,
        snapshots: tuple[_DeleteSnapshot, ...],
    ) -> None:
        for snapshot in snapshots:
            if _lexists(snapshot.view_path):
                continue
            relative = snapshot.view_path.relative_to(self.root)
            if snapshot.lower_link:
                self._create_whiteout(relative)
                continue

            self._remove_whiteout(relative)
            snapshot.view_path.parent.mkdir(parents=True, exist_ok=True)
            if snapshot.lower_path.is_file():
                try:
                    snapshot.view_path.symlink_to(snapshot.lower_path)
                except FileExistsError:
                    pass
        self._prepare_layout()

    def _is_in_view(self, path: Path) -> bool:
        return any(
            _is_relative_to(path, self.root / name) for name in _CAPABILITY_DIRECTORIES
        )

    def _lower_for_view_path(self, path: Path) -> Path | None:
        if not self._is_in_view(path):
            return None
        return self.repertoire / path.relative_to(self.root)

    def _reject_symlink_intermediates(self, path: Path) -> None:
        for name in _CAPABILITY_DIRECTORIES:
            view_root = self.root / name
            if not _is_relative_to(path, view_root):
                continue
            current = view_root
            for part in path.relative_to(view_root).parts[:-1]:
                current /= part
                if current.is_symlink():
                    raise ValueError(
                        "Capability View mutations must not traverse symbolic "
                        f"link directories: {current}"
                    )
                if not _lexists(current):
                    break
            return

    def _whiteout_path(self, relative: Path) -> Path:
        return self._whiteouts / relative

    def _create_whiteout(self, relative: Path) -> None:
        marker = self._whiteout_path(relative)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)

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


def _prepare_repertoire(repertoire: str | Path | None) -> Path:
    root = (
        Path.home() / ".config" / "cli-agent" / "repertoire"
        if repertoire is None
        else Path(repertoire).expanduser()
    ).resolve()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"cannot create Repertoire path: {root}") from exc
    _ensure_real_directory(root, label="Repertoire path")
    for name in _CAPABILITY_DIRECTORIES:
        _ensure_real_directory(
            root / name,
            label=f"Repertoire {name} capability path",
        )
    return root


def _managed_capability_path(path: str | Path) -> Path:
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


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_relative_to(first, second) or _is_relative_to(second, first)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _may_mutate(command: ShellParseResult) -> bool:
    return (
        command.contains_output_redirection
        or command.executable_basename in _DIRECT_MUTATORS
        and (
            command.executable_basename != "sed" or _sed_is_in_place(command.tokens[1:])
        )
    )


def _operands(tokens: tuple[str, ...]) -> list[str]:
    operands: list[str] = []
    options_done = False
    skip_redirection_target = False
    for token in tokens:
        if skip_redirection_target:
            skip_redirection_target = False
            continue
        if re.fullmatch(r"\d*(?:>>?|<>|>\|)", token):
            skip_redirection_target = True
            continue
        if re.match(r"^\d*(?:>>?|<>|>\|).+", token):
            continue
        if token in {"&&", "||", "|", "&", ";"}:
            break
        if token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("-"):
            continue
        operands.append(token)
    return operands
