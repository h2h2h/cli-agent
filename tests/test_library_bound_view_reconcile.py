"""Bound Capability View Library reconcile, mutation, and worker barrier tests.

RFC-0012 issue 07: the Library Catalog discovers, fingerprints, invalidates,
summarizes, and projects using only the Bound Capability View and the
Workspace Filesystem. These tests run the full lifecycle against an in-memory
Bound View and an in-memory Filesystem — no Host ``Path`` I/O, no live
Workspace, and no symlink mechanics.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ScriptedModelProvider,
)
from cli_agent.runtime._backend import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
)
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache

_LOWER: dict[str, bytes | None] = {
    "library/notes.md": b"lower note\n",
    "library/guide.md": b"guide\n",
    "library/memory": None,
    "library/memory/plan.md": b"plan\n",
}

_UPPER: dict[str, bytes | None] = {
    "library/notes.md": b"upper note\n",
    "library/local.md": b"local\n",
}

_WHITEOUTS = frozenset({"library/guide.md"})


class _InMemoryBoundView:
    """Effective Bound Capability View fake with no Host mechanics.

    ``None`` values mark directories. Provenance derives from membership in
    the lower/upper mappings (directories report lower presence, exactly like
    the Local Bound View); whiteouts are a plain set.
    """

    root = "/workspace"

    def __init__(
        self,
        lower: dict[str, bytes | None],
        upper: dict[str, bytes | None],
        whiteouts: frozenset[str] = frozenset(),
    ) -> None:
        self._lower = dict(lower)
        self._upper = dict(upper)
        self._whiteouts = set(whiteouts)
        self.calls: list[str] = []
        self.closed = False

    def _assert_open(self, operation: str) -> None:
        assert not self.closed, f"view accessed after close: {operation}"
        self.calls.append(operation)

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        self._assert_open("inspect")
        if relative_path in self._whiteouts:
            provenance: str | None = "whiteout"
        elif relative_path in self._upper:
            provenance = "workspace"
        elif relative_path in self._lower:
            provenance = "repertoire"
        else:
            provenance = None
        return _CapabilityInspection(
            relative_path=relative_path,
            provenance=provenance,
            shadows_repertoire=provenance == "workspace"
            and relative_path in self._lower,
            valid=True,
            validation_error=None,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        self._assert_open("list")
        prefix = relative_path.rstrip("/") + "/"
        children: dict[str, bytes | None] = {}
        for name, value in {**self._lower, **self._upper}.items():
            if name in self._whiteouts or not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :]
            first = remainder.split("/", 1)[0]
            if first not in children:
                children[first] = value if "/" not in remainder else None
        entries: list[_DirectoryEntry] = []
        for leaf, value in sorted(children.items()):
            kind = "directory" if value is None else "file"
            entries.append(
                _DirectoryEntry(
                    name=leaf,
                    metadata=_FileMetadata(kind=kind, size=0, mtime_ns=0, mode=0o644),
                )
            )
        return tuple(entries)

    async def read(self, relative_path: str) -> bytes:
        self._assert_open("read")
        effective = {**self._lower, **self._upper}
        content = effective.get(relative_path)
        if content is None or relative_path in self._whiteouts:
            raise _FilesystemError("not_found", f"no such file: {relative_path}")
        return content

    async def stat(self, relative_path: str) -> _FileMetadata:
        self._assert_open("stat")
        effective = {**self._lower, **self._upper}
        content = effective.get(relative_path)
        if relative_path in self._whiteouts or content is None:
            raise _FilesystemError("not_found", f"no such file: {relative_path}")
        return _FileMetadata(
            kind="directory" if content is None else "file",
            size=0,
            mtime_ns=0,
            mode=0o644,
        )


class _InMemoryFilesystem:
    """Minimal in-memory Workspace Filesystem capturing projection writes."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write(self, request: _FileWriteRequest) -> object:
        self.files[request.path] = request.content
        return request


def _cache(tmp_path: Path) -> _SummaryCache:
    return _SummaryCache(_StateDatabase.open(tmp_path / "state.sqlite3"))


def _completion(text: str) -> ModelCompletion:
    return ModelCompletion(message=AssistantMessage.text(text), finish_reason="stop")


def test_library_reconciles_against_in_memory_bound_view(tmp_path: Path) -> None:
    view = _InMemoryBoundView(_LOWER, _UPPER, _WHITEOUTS)
    filesystem = _InMemoryFilesystem()

    async def scenario() -> None:
        catalog = await _LibraryCatalog.reconcile(view, filesystem, _cache(tmp_path))

        assert isinstance(view, _LogicalCapabilityView)
        by_path = {entry.path: entry for entry in catalog.entries}
        assert by_path["notes.md"].provenance == "workspace"
        assert by_path["notes.md"].shadows_repertoire is True
        assert by_path["notes.md"].status == "pending"
        assert by_path["local.md"].provenance == "workspace"
        assert by_path["local.md"].shadows_repertoire is False
        assert by_path["memory"].kind == "directory"
        assert by_path["memory"].provenance == "repertoire"
        assert by_path["memory/plan.md"].provenance == "repertoire"
        assert "guide.md" not in by_path

        root_index = filesystem.files["/workspace/library/index.md"].decode("utf-8")
        assert "| notes.md | pending | workspace | yes |" in root_index
        assert "| local.md | pending | workspace | no |" in root_index
        assert "guide.md" not in root_index
        memory_index = filesystem.files["/workspace/library/memory/index.md"].decode(
            "utf-8"
        )
        assert "| plan.md | pending | repertoire | no |" in memory_index

        await catalog.close()

    asyncio.run(scenario())


def test_external_backend_mutation_is_reconciled_before_next_request(
    tmp_path: Path,
) -> None:
    view = _InMemoryBoundView(_LOWER, _UPPER, _WHITEOUTS)
    filesystem = _InMemoryFilesystem()

    async def scenario() -> None:
        catalog = await _LibraryCatalog.reconcile(view, filesystem, _cache(tmp_path))
        assert catalog.get("new.md") is None
        assert catalog.get("local.md") is not None

        view._upper["library/new.md"] = b"added\n"
        view._upper.pop("library/local.md")

        await catalog.reconcile_changes()

        assert catalog.get("new.md") is not None
        assert catalog.get("new.md").status == "pending"  # type: ignore[union-attr]
        assert catalog.get("local.md") is None
        await catalog.close()

    asyncio.run(scenario())


def test_worker_stops_touching_the_bound_view_after_close(
    tmp_path: Path,
) -> None:
    view = _InMemoryBoundView(_LOWER, _UPPER, _WHITEOUTS)
    filesystem = _InMemoryFilesystem()
    provider = ScriptedModelProvider(
        script=((_completion("Summary."),), (_completion("Root summary."),))
    )

    async def scenario() -> None:
        catalog = await _LibraryCatalog.reconcile(view, filesystem, _cache(tmp_path))
        catalog.start(provider)

        while not all(
            entry.status in {"ready", "failed"}
            for entry in catalog.entries
            if entry.path
        ):
            await asyncio.sleep(0.01)

        calls_before = len(view.calls)
        await catalog.close()
        view.closed = True
        calls_after_close = len(view.calls)
        await asyncio.sleep(0.01)

        assert len(view.calls) == calls_after_close
        assert calls_after_close >= calls_before

    asyncio.run(scenario())


def test_library_catalog_and_parser_never_touch_host_paths() -> None:
    for module_name in (
        "cli_agent.runtime._capability.library.catalog",
        "cli_agent.runtime._capability.library.parser",
    ):
        module = importlib.import_module(module_name)
        source = Path(module.__file__).read_text(encoding="utf-8")
        for token in (
            "pathlib",
            "read_bytes",
            "iterdir",
            "_atomic_write",
            "import os",
        ):
            assert token not in source, (module_name, token)
