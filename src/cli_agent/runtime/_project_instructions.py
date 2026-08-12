"""Workspace-root AGENTS.md discovery, validation, and snapshot model."""

from __future__ import annotations

import os
from dataclasses import dataclass

from cli_agent.runtime._backend.facts import _FilesystemError
from cli_agent.runtime._backend.protocol import _WorkspaceFilesystem

MAX_PROJECT_INSTRUCTION_BYTES = 32 * 1024

_AGENTS_MD_FILENAME = "AGENTS.md"


@dataclass(frozen=True, slots=True)
class _ProjectInstructions:
    """Immutable snapshot of one Workspace AGENTS.md loaded at Runtime open."""

    source: str
    text: str


async def _load_project_instructions(
    filesystem: _WorkspaceFilesystem,
    workspace: str,
) -> _ProjectInstructions | None:
    """Load and validate the Workspace root ``AGENTS.md`` as a Runtime snapshot.

    Only the Workspace root file is discovered. A missing path or a file
    containing only Unicode whitespace yields ``None``; every other invalid
    state raises a startup error that fails Runtime open.

    Args:
        filesystem (`_WorkspaceFilesystem`):
            Backend-neutral Workspace Filesystem used for stat and read.
        workspace (`str`):
            Resolved Workspace root path.

    Returns:
        The immutable project instruction snapshot, or ``None`` when the
        file is absent or contains only whitespace.

    Raises:
        ValueError: If the path is not a regular file, the raw bytes exceed
            the fixed 32 KiB limit, the content is not strict UTF-8, or the
            Backend fails to inspect or read the file.
    """

    source = os.path.join(workspace, _AGENTS_MD_FILENAME)
    try:
        metadata = await filesystem.stat(source)
    except _FilesystemError as exc:
        if exc.kind == "not_found":
            return None
        raise _startup_error("inspect", source, str(exc)) from exc
    if metadata.kind != "file":
        raise _startup_error(
            "inspect",
            source,
            f"expected a regular file, found {metadata.kind}",
        )
    if metadata.size > MAX_PROJECT_INSTRUCTION_BYTES:
        raise _startup_error(
            "size validation",
            source,
            f"exceeds the {MAX_PROJECT_INSTRUCTION_BYTES}-byte limit",
        )
    try:
        content = await filesystem.read(source)
    except _FilesystemError as exc:
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
    return _ProjectInstructions(source=source, text=text)


def _startup_error(operation: str, source: str, message: str) -> ValueError:
    """Build one startup error locating the failed operation and source path."""

    return ValueError(
        f"failed to {operation} project instructions at {source}: {message}"
    )
