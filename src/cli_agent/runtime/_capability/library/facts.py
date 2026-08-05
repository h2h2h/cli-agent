"""Pure-data Library capability facts shared across Runtime layers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

_LibraryStatus = Literal["ready", "pending", "stale", "failed", "unsupported"]

_SUMMARY_UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One effective Library path and its trusted Runtime-open facts.

    ``path`` is the logical POSIX path relative to the Library root; it never
    identifies an ``index.md`` projection. ``summary`` and ``error`` are absent
    in this milestone: summaries arrive with the background worker.
    """

    path: str
    kind: Literal["file", "directory"]
    provenance: Literal["repertoire", "workspace"] | None
    shadows_repertoire: bool
    fingerprint: str | None
    status: _LibraryStatus
    summary: str | None
    error: str | None


def _content_digest(source_bytes: bytes) -> str:
    """Return the stable digest of one Library source's raw bytes."""

    return hashlib.sha256(source_bytes).hexdigest()


def _file_fingerprint(source_digest: str) -> str:
    """Hash one file identity without name, path, model, prompt, or provenance."""

    return _fingerprint(("file", source_digest))


def _directory_fingerprint(
    children: tuple[tuple[str, str, str | None], ...],
) -> str:
    """Hash one directory identity from sorted direct-child facts.

    Args:
        children (`tuple[tuple[str, str, str | None], ...]`):
            Direct children sorted by name, each ``(name, kind, summary)``.
            ``None`` summaries use the Runtime-fixed unavailable text.

    Returns:
        A digest covering the directory domain separator and the ordered
        child name, kind, and summary triples.
    """

    parts = ["directory"]
    for name, kind, summary in children:
        parts.extend(
            (
                name,
                kind,
                _SUMMARY_UNAVAILABLE if summary is None else summary,
            )
        )
    return _fingerprint(tuple(parts))


def _fingerprint(parts: tuple[str, ...]) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    return hasher.hexdigest()
