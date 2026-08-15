"""Workspace-root AGENTS.md discovery, validation, and snapshot model.

The CapabilityProvider reads the Host Workspace root directly; the loader
never touches a Backend, Filesystem transport, or process.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

MAX_PROJECT_INSTRUCTION_BYTES = 32 * 1024

_AGENTS_MD_FILENAME = "AGENTS.md"


@dataclass(frozen=True, slots=True)
class _ProjectInstructions:
    """Immutable snapshot of one Workspace AGENTS.md loaded at Runtime open."""

    source: str
    text: str


def _load_project_instructions(workspace: Path) -> _ProjectInstructions | None:
    """Load and validate the Workspace root ``AGENTS.md`` from Host source.

    Only the Workspace root file is discovered. A missing path or a file
    containing only Unicode whitespace yields ``None``; every other invalid
    state raises a startup error that fails Runtime open.

    Args:
        workspace (`Path`):
            Resolved Host Workspace root.

    Returns:
        The immutable project instruction snapshot, or ``None`` when the
        file is absent or contains only whitespace.

    Raises:
        ValueError: If the path is not a regular file, the raw bytes exceed
            the fixed 32 KiB limit, or the content is not strict UTF-8.
    """

    source = workspace / _AGENTS_MD_FILENAME
    try:
        metadata = source.stat()
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            return None
        raise _startup_error("inspect", source, str(exc)) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _startup_error(
            "inspect",
            source,
            "expected a regular file",
        )
    if metadata.st_size > MAX_PROJECT_INSTRUCTION_BYTES:
        raise _startup_error(
            "size validation",
            source,
            f"exceeds the {MAX_PROJECT_INSTRUCTION_BYTES}-byte limit",
        )
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise _startup_error("read", source, str(exc)) from exc
    if len(content) > MAX_PROJECT_INSTRUCTION_BYTES:
        raise _startup_error(
            "size validation",
            source,
            f"exceeds the {MAX_PROJECT_INSTRUCTION_BYTES}-byte limit",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _startup_error("decode", source, "content is not valid UTF-8") from exc
    if not text.strip():
        return None
    return _ProjectInstructions(source=str(source), text=text)


def _startup_error(operation: str, source: Path, message: str) -> ValueError:
    """Build one startup error locating the failed operation and source path."""

    return ValueError(
        f"failed to {operation} project instructions at {source}: {message}"
    )
