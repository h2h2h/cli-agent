"""Mutable Runtime-owned Library Catalog facts from the effective view."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

from cli_agent.runtime._capability.library.cache import _SummaryCache
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
from cli_agent.runtime._capability.workspace import _atomic_write

_INDEX_FILENAME = "index.md"
_LIBRARY_DIRECTORY = "library"

_PENDING_DESCRIPTIONS = {
    "file": "Summary generation pending.",
    "directory": "Directory summary generation pending.",
}
_UNSUPPORTED_DESCRIPTION = "Unsupported format; read the source file directly."
_FAILED_FALLBACK_DESCRIPTION = "Summary generation failed."
_STALE_FALLBACK_DESCRIPTION = "Summary is stale; regeneration pending."


class _LibraryCatalog:
    """Reference-stable mutable facts and generated indexes for the Library.

    Reconcile never calls a model: cache hits become ``ready`` and every
    visible directory gets an atomically written ``index.md`` projection
    during Runtime open. Later milestones add worker state to this object.
    """

    def __init__(
        self,
        entries: tuple[LibraryEntry, ...],
        root: Path,
        summary_cache: _SummaryCache,
    ) -> None:
        """Hold the facts, the effective Library root, and the summary cache."""

        self.entries = entries
        self._root = root
        self._summary_cache = summary_cache
        self._by_path = {entry.path: entry for entry in entries}

    @classmethod
    async def reconcile(
        cls,
        capability_view: _CapabilityView,
        summary_cache: _SummaryCache,
    ) -> _LibraryCatalog:
        """Discover facts, resolve cache hits, and render every index.

        Args:
            capability_view (`_CapabilityView`):
                The opened Capability View; ``library`` is read as an ordinary
                capability directory with no source-layer restrictions.
            summary_cache (`_SummaryCache`):
                The application state summary cache; file fingerprints with
                cached summaries become ``ready`` before the first render.

        Returns:
            A catalog of trusted facts whose ``index.md`` projections have
            been written from the deepest directory up to the root.
        """

        root = capability_view.root / _LIBRARY_DIRECTORY
        if not root.is_dir():
            return cls((), root, summary_cache)
        discovered: list[LibraryEntry] = []
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if child.name != _INDEX_FILENAME:
                discovered.extend(await _subtree(capability_view, root, child))
        catalog = cls(
            _apply_cache_hits(tuple(discovered), summary_cache), root, summary_cache
        )
        catalog.render_indexes()
        return catalog

    def get(self, path: str) -> LibraryEntry | None:
        """Return the entry for one logical Library path, or None."""

        return self._by_path.get(path)

    def close(self) -> None:
        """Close the underlying summary cache and state database."""

        self._summary_cache.close()

    def render_indexes(self) -> None:
        """Atomically write every visible directory index, deepest first.

        The root index is written last. Each replacement is atomic on its own;
        the whole set is only eventually consistent.
        """

        directories = [
            entry.path for entry in self.entries if entry.kind == "directory"
        ]
        directories.append("")
        for directory in sorted(directories, key=lambda path: (-_depth(path), path)):
            _atomic_write(
                self._root / directory / _INDEX_FILENAME,
                self.render_index(directory).encode("utf-8"),
            )

    def render_index(self, directory: str) -> str:
        """Render one directory projection without creating authority.

        Args:
            directory (`str`):
                The logical directory path; ``""`` renders the Library root.

        Returns:
            Deterministic Markdown listing only direct children as tables,
            with stable frontmatter and no chunks, body previews, or hidden
            metadata.
        """

        entry = self._by_path.get(directory)
        lines = [
            "---",
            f"name: {_markdown_cell(_leaf(directory)) if directory else 'library'}",
            f"path: {_markdown_cell('library' + ('/' + directory if directory else ''))}",
            "type: dir",
            f"status: {'pending' if entry is None else entry.status}",
            f"description: {_markdown_cell(_description(entry))}",
            "---",
            "",
            "## Directories",
            "",
        ]
        children = sorted(
            (
                candidate
                for candidate in self.entries
                if _parent(candidate.path) == directory
            ),
            key=lambda candidate: _leaf(candidate.path),
        )
        subdirectories = [child for child in children if child.kind == "directory"]
        if subdirectories:
            lines.extend(
                ["| Name | Status | Description | Index |", "|---|---|---|---|"]
            )
            for child in subdirectories:
                name = _markdown_cell(_leaf(child.path))
                lines.append(
                    "| {name} | {status} | {description} | "
                    "[{name}](./{name}/index.md) |".format(
                        name=name,
                        status=child.status,
                        description=_markdown_cell(_description(child)),
                    )
                )
        else:
            lines.append("_no directories_")
        lines.extend(["", "## Files", ""])
        files = [child for child in children if child.kind == "file"]
        if files:
            lines.extend(
                [
                    "| Name | Status | Provenance | Shadows Repertoire | "
                    "Description | File |",
                    "|---|---|---|---|---|---|",
                ]
            )
            for child in files:
                name = _markdown_cell(_leaf(child.path))
                lines.append(
                    "| {name} | {status} | {provenance} | {shadows} | "
                    "{description} | [{name}](./{name}) |".format(
                        name=name,
                        status=child.status,
                        provenance=child.provenance or "unknown",
                        shadows="yes" if child.shadows_repertoire else "no",
                        description=_markdown_cell(_description(child)),
                    )
                )
        else:
            lines.append("_no files_")
        return "\n".join(lines) + "\n"


def _apply_cache_hits(
    entries: tuple[LibraryEntry, ...],
    summary_cache: _SummaryCache,
) -> tuple[LibraryEntry, ...]:
    """Turn pending files with cached summaries into ready entries."""

    fingerprints = tuple(
        fingerprint
        for entry in entries
        if entry.kind == "file"
        and entry.status == "pending"
        and (fingerprint := entry.fingerprint) is not None
    )
    hits = summary_cache.get(fingerprints)
    if not hits:
        return entries
    return tuple(
        replace(entry, status="ready", summary=hits[entry.fingerprint])
        if entry.fingerprint in hits
        else entry
        for entry in entries
    )


def _description(entry: LibraryEntry | None) -> str:
    """Return the bounded description text for one index entry."""

    if entry is None:
        return _PENDING_DESCRIPTIONS["directory"]
    if entry.status == "unsupported":
        return _UNSUPPORTED_DESCRIPTION
    if entry.status == "failed" and entry.error is not None:
        return entry.error
    if entry.summary is not None:
        return entry.summary
    if entry.status == "failed":
        return _FAILED_FALLBACK_DESCRIPTION
    if entry.status == "stale":
        return _STALE_FALLBACK_DESCRIPTION
    return _PENDING_DESCRIPTIONS[entry.kind]


def _markdown_cell(value: str) -> str:
    """Escape one value so it is safe inside one Markdown table cell."""

    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _depth(path: str) -> int:
    return path.count("/") + 1 if path else 0


def _leaf(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


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
