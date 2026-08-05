"""Mutable Runtime-owned Library Catalog facts from the effective view."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from cli_agent.runtime._capability.library.facts import (
    LibraryEntry,
    _content_digest,
    _file_fingerprint,
)
from cli_agent.runtime._capability.library.parser import (
    LibraryParseError,
    _select_parser,
)
from cli_agent.runtime._capability.view import _CapabilityView

_INDEX_FILENAME = "index.md"
_LIBRARY_DIRECTORY = "library"


class _LibraryCatalog:
    """Reference-stable mutable facts for the effective Library.

    This milestone reconciles facts only: it never calls a model, queries a
    cache, or renders ``index.md`` projections. Later milestones add worker
    state and renderers to the same object.
    """

    def __init__(self, entries: tuple[LibraryEntry, ...]) -> None:
        """Hold the immutable fact tuple and index entries by logical path."""

        self.entries = entries
        self._by_path = {entry.path: entry for entry in entries}

    @classmethod
    async def reconcile(cls, capability_view: _CapabilityView) -> _LibraryCatalog:
        """Discover effective Library facts without any model work.

        Args:
            capability_view (`_CapabilityView`):
                The opened Capability View; ``library`` is read as an ordinary
                capability directory with no source-layer restrictions.

        Returns:
            A catalog of trusted facts for every visible Library path.
        """

        root = capability_view.root / _LIBRARY_DIRECTORY
        if not root.is_dir():
            return cls(())
        entries: list[LibraryEntry] = []
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if child.name != _INDEX_FILENAME:
                entries.extend(await _subtree(capability_view, root, child))
        return cls(tuple(entries))

    def get(self, path: str) -> LibraryEntry | None:
        """Return the entry for one logical Library path, or None."""

        return self._by_path.get(path)


async def _subtree(
    capability_view: _CapabilityView,
    root: Path,
    path: Path,
) -> tuple[LibraryEntry, ...]:
    """Return trusted facts for one discovered path and its subtree.

    ``index.md`` names are excluded at every level. Whiteouted or vanished
    paths contribute nothing. Inspection and listing failures only affect the
    corresponding entry.
    """

    relative = path.relative_to(root)
    try:
        inspection = capability_view.inspect(Path(_LIBRARY_DIRECTORY) / relative)
    except ValueError as exc:
        return (
            _entry(
                relative,
                "directory" if path.is_dir() else "file",
                provenance=None,
                shadows_repertoire=False,
                fingerprint=None,
                status="failed",
                error=str(exc),
            ),
        )
    if inspection.provenance not in {"repertoire", "workspace"}:
        return ()
    if path.is_dir():
        return await _directory_subtree(capability_view, root, path, relative)
    return (
        await _file_entry(
            path,
            relative,
            inspection.provenance,
            inspection.shadows_repertoire,
        ),
    )


def _directory_facts(
    capability_view: _CapabilityView,
    relative: Path,
) -> tuple[Literal["repertoire", "workspace"], bool]:
    """Return one directory's presence layer and its shadow fact.

    Workspace and Repertoire directories merge in the view instead of
    shadowing, so ``shadows_repertoire`` is always false for directories. A
    lower presence marks the directory as ``repertoire``; otherwise it is
    Workspace-owned.
    """

    lower = capability_view.repertoire / _LIBRARY_DIRECTORY / relative
    return ("repertoire" if lower.is_dir() else "workspace", False)


async def _directory_subtree(
    capability_view: _CapabilityView,
    root: Path,
    directory: Path,
    relative: Path,
) -> tuple[LibraryEntry, ...]:
    """Return one directory fact followed by its direct-child subtree facts."""

    provenance, shadows = _directory_facts(capability_view, relative)
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        return (
            _entry(
                relative,
                "directory",
                provenance=provenance,
                shadows_repertoire=shadows,
                fingerprint=None,
                status="failed",
                error=f"cannot list directory: {exc}",
            ),
        )
    child_entries: list[LibraryEntry] = []
    for child in children:
        if child.name != _INDEX_FILENAME:
            child_entries.extend(await _subtree(capability_view, root, child))
    return (
        _entry(
            relative,
            "directory",
            provenance=provenance,
            shadows_repertoire=shadows,
            fingerprint=None,
            status="pending",
            error=None,
        ),
        *child_entries,
    )


async def _file_entry(
    path: Path,
    relative: Path,
    provenance: Literal["repertoire", "workspace"],
    shadows_repertoire: bool,
) -> LibraryEntry:
    try:
        source = path.read_bytes()
    except OSError as exc:
        return _entry(
            relative,
            "file",
            provenance=provenance,
            shadows_repertoire=shadows_repertoire,
            fingerprint=None,
            status="failed",
            error=f"cannot read file: {exc}",
        )
    fingerprint = _file_fingerprint(_content_digest(source))
    parser = _select_parser(path)
    if parser is None:
        return _entry(
            relative,
            "file",
            provenance=provenance,
            shadows_repertoire=shadows_repertoire,
            fingerprint=fingerprint,
            status="unsupported",
            error=f"no parser supports file type: {path.name}",
        )
    try:
        await parser.parse(path)
    except LibraryParseError as exc:
        return _entry(
            relative,
            "file",
            provenance=provenance,
            shadows_repertoire=shadows_repertoire,
            fingerprint=fingerprint,
            status="failed",
            error=str(exc),
        )
    return _entry(
        relative,
        "file",
        provenance=provenance,
        shadows_repertoire=shadows_repertoire,
        fingerprint=fingerprint,
        status="pending",
        error=None,
    )


def _entry(
    relative: Path,
    kind: Literal["file", "directory"],
    *,
    provenance: Literal["repertoire", "workspace"] | None,
    shadows_repertoire: bool,
    fingerprint: str | None,
    status: Literal["ready", "pending", "stale", "failed", "unsupported"],
    error: str | None,
) -> LibraryEntry:
    return LibraryEntry(
        path=relative.as_posix(),
        kind=kind,
        provenance=provenance,
        shadows_repertoire=shadows_repertoire,
        fingerprint=fingerprint,
        status=status,
        summary=None,
        error=error,
    )
