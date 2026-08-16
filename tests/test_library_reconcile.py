import asyncio
import shutil
from pathlib import Path

from cli_agent._adapters.local.view import _LocalCapabilityView
from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ScriptedModelProvider,
)
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalWorkspaceFilesystem,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.library.facts import (
    _content_digest,
    _file_fingerprint,
)
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.sources import _FileSource


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def _cache(workspace: Path) -> _SummaryCache:
    return _SummaryCache(_StateDatabase.open(workspace / "state.sqlite3"))


def _fingerprint_of(content: str) -> str:
    return _file_fingerprint(_content_digest(content.encode("utf-8")))


def _completion(text: str) -> ModelCompletion:
    return ModelCompletion(message=AssistantMessage.text(text), finish_reason="stop")


def _library(workspace: Path) -> Path:
    return workspace / ".workspace" / "library"


def _index(workspace: Path, *parts: str) -> str:
    return (_library(workspace) / Path(*parts) / "index.md").read_text(encoding="utf-8")


class _RecordingProvider(ScriptedModelProvider):
    """Scripted provider yielding one text completion per configured summary."""

    def __init__(self, script: tuple[str, ...]) -> None:
        super().__init__(
            (
                (
                    ModelCompletion(
                        message=AssistantMessage.text(summary), finish_reason="stop"
                    ),
                )
                for summary in script
            )
        )


async def _drain(catalog: _LibraryCatalog) -> None:
    await catalog._queue.join()  # type: ignore[union-attr]


def _scenario(workspace: Path, repertoire: Path) -> _LocalCapabilityView:
    _prepare_workspace(workspace)
    return _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)


def _filesystem(
    workspace: Path,
    view: _LocalCapabilityView,
) -> _LocalWorkspaceFilesystem:
    backend = _LocalBackendWorkspace(workspace, {})
    return backend.filesystem


async def _reconcile_catalog(
    view: _LocalCapabilityView,
    workspace: Path,
) -> _LibraryCatalog:
    return await _LibraryCatalog.reconcile(
        view,
        _filesystem(workspace, view),
        _cache(workspace),
    )


async def _write_library_file(
    workspace: Path,
    catalog: _LibraryCatalog,
    view: _LocalCapabilityView,
    logical: str,
    content: str,
) -> None:
    """Write one Library file through the real Files command handler."""

    handler = _FileSource(
        _filesystem(workspace, view),
        mark_dirty=catalog.mark_path_dirty,
    )
    execution = handler.prepare(
        _ExecutionRequest(
            command=parse_shell_ast(f"files write .workspace/library/{logical}"),
            stdin=content,
        ),
        _CommandContext(
            workspace=str(workspace),
            cwd=str(workspace),
            environment={},
        ),
    )
    await execution.run(_DiscardOutput())


def test_external_edit_transitions_ready_to_stale_then_converges(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "notes.md").write_text("one\n", encoding="utf-8")

    provider = _RecordingProvider(
        script=("one", "Root holds one file.", "two", "Root holds two files.")
    )

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        catalog.start(provider)
        await _drain(catalog)
        assert catalog.get("notes.md").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]

        (_library(tmp_path) / "notes.md").write_text("two\n", encoding="utf-8")
        await catalog.reconcile_changes()

        notes = catalog.get("notes.md")
        root = catalog.get("")
        assert notes is not None and root is not None
        assert notes.status == "stale"
        assert notes.summary == "one"
        assert root.status == "stale"
        assert root.summary == "Root holds one file."
        assert "one" in _index(tmp_path)
        assert len(provider.requests) == 2

        await _drain(catalog)
        notes = catalog.get("notes.md")
        root = catalog.get("")
        assert notes is not None and root is not None
        assert notes.status == "ready"
        assert notes.summary == "two"
        assert root.status == "ready"
        assert root.summary == "Root holds two files."
        await catalog.close()

    asyncio.run(scenario())


def test_external_new_file_is_pending_and_reconcile_calls_no_model(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    provider = _RecordingProvider(script=("new summary.", "Root holds one file."))

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("").summary == "Empty directory."  # type: ignore[union-attr]

        (_library(tmp_path) / "added.md").write_text("new\n", encoding="utf-8")
        await catalog.reconcile_changes()

        added = catalog.get("added.md")
        assert added is not None
        assert added.kind == "file"
        assert added.status == "pending"
        assert provider.requests == ()
        assert "Summary generation pending." in _index(tmp_path)

        catalog.start(provider)
        await _drain(catalog)
        assert catalog.get("added.md").status == "ready"  # type: ignore[union-attr]
        await catalog.close()

    asyncio.run(scenario())


def test_external_deletion_removes_entry_and_updates_index(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "notes.md").write_text("one\n", encoding="utf-8")
    provider = _RecordingProvider(script=("one", "Root holds one file."))

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        catalog.start(provider)
        await _drain(catalog)

        (_library(tmp_path) / "notes.md").unlink()
        await catalog.reconcile_changes()

        assert catalog.get("notes.md") is None
        root = catalog.get("")
        assert root is not None
        assert root.status == "ready"
        assert root.summary == "Empty directory."
        assert "_no files_" in _index(tmp_path)
        assert "notes" not in _index(tmp_path)
        await catalog.close()

    asyncio.run(scenario())


def test_external_new_directory_subtree_is_discovered(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    provider = _RecordingProvider(script=("new summary.", "Root holds one file."))

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)

        (Path(view.root) / "library" / "newdir").mkdir()
        (Path(view.root) / "library" / "newdir" / "a.md").write_text(
            "a\n", encoding="utf-8"
        )
        (Path(view.root) / "library" / "newdir" / "empty").mkdir()
        await catalog.reconcile_changes()

        by_path = {entry.path: entry for entry in catalog.entries}
        assert set(by_path) == {"newdir", "newdir/a.md", "newdir/empty"}
        assert by_path["newdir/a.md"].status == "pending"
        assert by_path["newdir/empty"].status == "ready"
        assert by_path["newdir/empty"].summary == "Empty directory."
        assert by_path["newdir"].status == "pending"
        assert provider.requests == ()
        await catalog.close()

    asyncio.run(scenario())


def test_external_directory_deletion_removes_whole_subtree(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "newdir").mkdir()
    (repertoire / "library" / "newdir" / "a.md").write_text("a\n", encoding="utf-8")

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        assert catalog.get("newdir/a.md") is not None

        shutil.rmtree(Path(view.root) / "library" / "newdir")
        await catalog.reconcile_changes()

        assert catalog.get("newdir") is None
        assert catalog.get("newdir/a.md") is None
        assert set(catalog.entries) == set()
        await catalog.close()

    asyncio.run(scenario())


def test_files_write_marks_dirty_path_and_next_reconcile_applies(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    provider = _RecordingProvider(script=("written.", "Root holds one file."))

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        await _write_library_file(tmp_path, catalog, view, "notes.md", "fresh\n")

        assert catalog._dirty_paths == {"notes.md"}
        assert catalog.get("notes.md") is None

        await catalog.reconcile_changes()

        assert catalog._dirty_paths == set()
        notes = catalog.get("notes.md")
        assert notes is not None
        assert notes.status == "pending"
        assert provider.requests == ()
        for entry in catalog.entries:
            assert "dirty" not in entry.status
        assert "dirty" not in _index(tmp_path)

        catalog.start(provider)
        await _drain(catalog)
        assert catalog.get("notes.md").status == "ready"  # type: ignore[union-attr]
        await catalog.close()

    asyncio.run(scenario())


def test_files_edit_marks_dirty_and_transitions_to_stale(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "notes.md").write_text("one\n", encoding="utf-8")
    provider = _RecordingProvider(
        script=("one", "Root holds one file.", "one two", "Root holds two files.")
    )

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        catalog.start(provider)
        await _drain(catalog)
        assert catalog.get("notes.md").status == "ready"  # type: ignore[union-attr]

        handler = _FileSource(
            _filesystem(tmp_path, view),
            mark_dirty=catalog.mark_path_dirty,
        )
        execution = handler.prepare(
            _ExecutionRequest(
                command=parse_shell_ast("files edit .workspace/library/notes.md"),
                stdin='{"edits": [{"oldText": "one\\n", "newText": "one two\\n"}]}',
            ),
            _CommandContext(
                workspace=str(tmp_path),
                cwd=str(tmp_path),
                environment={},
            ),
        )
        await execution.run(_DiscardOutput())

        assert catalog._dirty_paths == {"notes.md"}
        await catalog.reconcile_changes()

        notes = catalog.get("notes.md")
        assert notes is not None
        assert notes.status == "stale"
        assert notes.summary == "one"
        assert "one" in _index(tmp_path)

        await _drain(catalog)
        assert catalog.get("notes.md").summary == "one two"  # type: ignore[union-attr]
        assert catalog.get("notes.md").status == "ready"  # type: ignore[union-attr]
        await catalog.close()

    asyncio.run(scenario())


def test_failed_files_write_never_marks_dirty(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        (tmp_path / "blocker.txt").write_text("occupied\n", encoding="utf-8")

        handler = _FileSource(
            _filesystem(tmp_path, view),
            mark_dirty=catalog.mark_path_dirty,
        )
        execution = handler.prepare(
            _ExecutionRequest(
                command=parse_shell_ast("files write blocker.txt/nested.md"),
                stdin="content\n",
            ),
            _CommandContext(
                workspace=str(tmp_path),
                cwd=str(tmp_path),
                environment={},
            ),
        )
        outcome = await execution.run(_DiscardOutput())

        assert outcome == 1
        assert catalog._dirty_paths == set()
        await catalog.close()

    asyncio.run(scenario())


def test_cache_hit_restores_ready_without_the_worker(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "notes.md").write_text("one\n", encoding="utf-8")
    provider = _RecordingProvider(script=("new summary.", "Root holds one file."))

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        cache = _cache(tmp_path)
        cache.upsert(_fingerprint_of("cached\n"), "file", "Cached summary.")
        catalog = await _LibraryCatalog.reconcile(
            view, _filesystem(tmp_path, view), cache
        )

        (_library(tmp_path) / "notes.md").write_text("cached\n", encoding="utf-8")
        await catalog.reconcile_changes()

        notes = catalog.get("notes.md")
        assert notes is not None
        assert notes.status == "ready"
        assert notes.summary == "Cached summary."
        assert provider.requests == ()
        await catalog.close()

    asyncio.run(scenario())


def test_whiteouted_file_is_removed(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "notes.md").write_text("one\n", encoding="utf-8")

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        assert catalog.get("notes.md") is not None

        (_library(tmp_path) / "notes.md").unlink()
        marker = view._whiteouts / "library" / "notes.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        await catalog.reconcile_changes()

        assert catalog.get("notes.md") is None
        await catalog.close()

    asyncio.run(scenario())


def test_files_write_over_repertoire_file_becomes_workspace_override(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "guide.md").write_text("lower\n", encoding="utf-8")

    async def scenario() -> None:
        view = _scenario(tmp_path, repertoire)
        catalog = await _reconcile_catalog(view, tmp_path)
        view_md = Path(view.root) / "library" / "guide.md"
        assert view_md.is_symlink()

        await _write_library_file(tmp_path, catalog, view, "guide.md", "upper\n")
        await catalog.reconcile_changes()

        entry = catalog.get("guide.md")
        assert entry is not None
        assert entry.provenance == "workspace"
        assert entry.shadows_repertoire is True
        assert (repertoire / "library" / "guide.md").read_text() == "lower\n"
        await catalog.close()

    asyncio.run(scenario())


class _DiscardOutput:
    async def write(self, stream: str, data: bytes) -> None:
        del stream, data
