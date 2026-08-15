"""Host-owned Capability Source and State preparation.

RFC-0012 separates the logical Capability lower/upper/whiteout inputs from
their materialization. These Host preparation helpers own the persistent
``Path`` inputs: they create or validate the Repertoire lower tree and
reject a Repertoire that overlaps the Workspace state directory. Bound View
materialization happens later inside the CapabilityDeployment plane.
"""

from __future__ import annotations

from pathlib import Path

from cli_agent.runtime._capability.workspace import _ensure_real_directory

_CAPABILITY_DIRECTORIES = ("tools", "skills", "library", "_mcp")

_MCP_DIRECTORY = "_mcp"


def _prepare_capability_source(
    repertoire: str | Path | None,
    state_root: Path,
) -> Path:
    """Open the Host Repertoire and return its resolved root path.

    Args:
        repertoire (`str | Path | None`):
            User-maintained capability lower tree; defaults to
            ``~/.cli-agent/repertoire``.
        state_root (`Path`):
            Resolved Workspace state directory the Repertoire must not
            overlap.

    Returns:
        The resolved Repertoire root consumed by the CapabilityDeployment.

    Raises:
        ValueError: If the Repertoire path cannot be created, is not a real
            directory, or overlaps the Workspace state directory.
    """

    root = (
        Path.home() / ".cli-agent" / "repertoire"
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
    if _paths_overlap(state_root, root):
        raise ValueError("repertoire must be outside the Workspace state directory")
    return root


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_relative_to(first, second) or _is_relative_to(second, first)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
