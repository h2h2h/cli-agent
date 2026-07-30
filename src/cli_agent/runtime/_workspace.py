"""Persistent Workspace-open path preparation."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from dotenv.parser import parse_stream

_WORKSPACE_STATE_DIRECTORY = ".workspace"
_WORKSPACE_ENVIRONMENT_FILE = "env"


@dataclass(frozen=True, slots=True)
class _WorkspacePaths:
    root: Path
    state: Path
    environment: Path


def _prepare_workspace(workspace: str | Path) -> _WorkspacePaths:
    """Resolve a Workspace and idempotently establish its persistent namespace."""

    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace must be an existing directory: {workspace}")

    state = root / _WORKSPACE_STATE_DIRECTORY
    _ensure_real_directory(state, label="workspace state path")

    environment = state / _WORKSPACE_ENVIRONMENT_FILE
    _ensure_real_file(environment, label="workspace environment file")

    return _WorkspacePaths(
        root=root,
        state=state,
        environment=environment,
    )


def _load_workspace_environment(environment: Path) -> Mapping[str, str]:
    """Load one complete dotenv mapping without mutating ``os.environ``."""

    try:
        entry_stat = environment.lstat()
    except OSError as exc:
        raise ValueError(
            f"cannot inspect workspace environment file: {environment}"
        ) from exc
    if not stat.S_ISREG(entry_stat.st_mode):
        raise ValueError(
            f"workspace environment path must be a regular dotenv file: {environment}"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(environment, flags)
    except OSError as exc:
        raise ValueError(
            f"cannot open workspace environment file: {environment}"
        ) from exc

    loaded: dict[str, str] = {}
    try:
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            opened_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened_stat.st_mode) or (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) != (entry_stat.st_dev, entry_stat.st_ino):
                raise ValueError(
                    f"workspace environment file changed while opening: {environment}"
                )

            for binding in parse_stream(stream):
                if binding.error:
                    raise ValueError(
                        "invalid dotenv syntax in workspace environment file "
                        f"at line {binding.original.line}: {environment}"
                    )
                if binding.key is None:
                    continue
                if binding.value is None:
                    raise ValueError(
                        "workspace environment variable must use KEY=VALUE "
                        f"at line {binding.original.line}: {environment}"
                    )
                if "\x00" in binding.key or "\x00" in binding.value:
                    raise ValueError(
                        "workspace environment file must not contain NUL "
                        f"at line {binding.original.line}: {environment}"
                    )
                loaded[binding.key] = binding.value
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"workspace environment file must contain valid UTF-8: {environment}"
        ) from exc

    return MappingProxyType(loaded)


def _ensure_real_directory(path: Path, *, label: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(f"cannot create {label}: {path}") from exc

    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a real directory: {path}")


def _ensure_real_file(path: Path, *, label: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(f"cannot create {label}: {path}") from exc
    else:
        os.close(descriptor)

    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a real regular file: {path}")
