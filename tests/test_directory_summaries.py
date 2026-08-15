import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    RuntimeDiagnostic,
    ScriptedModelProvider,
)
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalCapabilityView,
)
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.library.facts import (
    _content_digest,
    _directory_fingerprint,
    _file_fingerprint,
)
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime.model import ModelContextOverflowSignal


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


def _index(workspace: Path, *parts: str) -> str:
    return (workspace / ".workspace" / "library" / Path(*parts) / "index.md").read_text(
        encoding="utf-8"
    )


class _FailOnCallProvider:
    """Succeed on every request except one configured call index."""

    def __init__(self, fail_on: int, error: BaseException) -> None:
        self._fail_on = fail_on
        self._error = error
        self._calls = 0
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self._requests.append(request)
        self._calls += 1
        if self._calls == self._fail_on:
            raise self._error
        yield _completion(f"Summary of call {self._calls}.")


async def _drain(catalog: _LibraryCatalog) -> None:
    await catalog._queue.join()  # type: ignore[union-attr]


def _user_text(request: ModelRequest) -> str:
    return request.messages[1].content[0].text


def test_multi_level_directories_converge_bottom_up(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "d1").mkdir()
    (repertoire / "library" / "d1" / "f1.md").write_text(
        "f1 content\n", encoding="utf-8"
    )
    (repertoire / "library" / "d2" / "d3").mkdir(parents=True)
    (repertoire / "library" / "d2" / "d3" / "f2.md").write_text(
        "f2 content\n", encoding="utf-8"
    )
    provider = ScriptedModelProvider(
        script=(
            (_completion("Summary of f1."),),
            (_completion("Summary of f2."),),
            (_completion("Summary of d1."),),
            (_completion("Summary of d3."),),
            (_completion("Summary of d2."),),
            (_completion("Summary of root."),),
        )
    )

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(provider)
        await _drain(catalog)

        assert catalog.get("d1").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("d2").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("d2/d3").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("").summary == "Summary of root."  # type: ignore[union-attr]

        users = [_user_text(request) for request in provider.requests]
        assert users[0] == "The file content is:\n\nf1 content\n"
        assert users[1] == "The file content is:\n\nf2 content\n"
        assert users[2] == "- name: f1.md | type: file | summary: Summary of f1."
        assert users[3] == "- name: f2.md | type: file | summary: Summary of f2."
        assert users[4] == "- name: d3 | type: directory | summary: Summary of d3."
        assert users[5] == (
            "- name: d1 | type: directory | summary: Summary of d1.\n"
            "- name: d2 | type: directory | summary: Summary of d2."
        )
        assert "f1 content" not in users[5]
        for request in provider.requests:
            assert request.tools == ()

        d1_frontmatter = _index(tmp_path, "d1").splitlines()[:6]
        assert d1_frontmatter[4:] == ["status: ready", "description: Summary of d1."]
        root_frontmatter = _index(tmp_path).splitlines()[:6]
        assert root_frontmatter[4:] == [
            "status: ready",
            "description: Summary of root.",
        ]

        await catalog.close()

    asyncio.run(scenario())


def test_directory_cache_hit_skips_model_call(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "d1").mkdir()
    (repertoire / "library" / "d1" / "f1.md").write_text(
        "content-1\n", encoding="utf-8"
    )

    cache = _cache(tmp_path)
    cache.upsert(_fingerprint_of("content-1\n"), "file", "F1 summary.")
    cache.upsert(
        _directory_fingerprint((("f1.md", "file", "F1 summary."),)),
        "directory",
        "D1 summary.",
    )
    cache.upsert(
        _directory_fingerprint((("d1", "directory", "D1 summary."),)),
        "directory",
        "Root summary.",
    )
    cache.close()

    provider = ScriptedModelProvider(script=())

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        assert catalog.get("d1").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("d1").summary == "D1 summary."  # type: ignore[union-attr]
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]

        catalog.start(provider)
        await _drain(catalog)

        assert provider.requests == ()
        await catalog.close()

    asyncio.run(scenario())


def test_failed_child_uses_unavailable_text(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "d1").mkdir()
    (repertoire / "library" / "d1" / "bad.md").write_text("content\n", encoding="utf-8")
    provider = _FailOnCallProvider(1, RuntimeError("provider exploded"))

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(provider)
        await _drain(catalog)

        assert catalog.get("d1/bad.md").status == "failed"  # type: ignore[union-attr]
        assert catalog.get("d1").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]
        requests = provider.requests
        assert _user_text(requests[1]) == (
            "- name: bad.md | type: file | summary: unavailable"
        )
        await catalog.close()

    asyncio.run(scenario())


def test_directory_becomes_stale_then_ready_when_child_changes(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "d1").mkdir()
    (repertoire / "library" / "d1" / "f1.md").write_text("content\n", encoding="utf-8")

    cache = _cache(tmp_path)
    cache.upsert(_fingerprint_of("content\n"), "file", "A1")
    cache.upsert(
        _directory_fingerprint((("f1.md", "file", "A1"),)),
        "directory",
        "D1-old",
    )
    cache.upsert(
        _directory_fingerprint((("d1", "directory", "D1-old"),)),
        "directory",
        "R-old",
    )
    cache.close()

    provider = ScriptedModelProvider(
        script=(
            (_completion("D1-new"),),
            (_completion("R-new"),),
        )
    )

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(provider)
        assert catalog.get("d1").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]

        f1 = catalog.get("d1/f1.md")
        assert f1 is not None
        catalog._entries["d1/f1.md"] = replace(f1, summary="A2")
        await catalog._cascade("d1/f1.md")

        d1 = catalog.get("d1")
        assert d1 is not None
        assert d1.status == "stale"
        assert d1.summary == "D1-old"
        assert d1.error is None
        root = catalog.get("")
        assert root is not None
        assert root.status == "stale"
        assert root.summary == "R-old"

        await _drain(catalog)

        d1 = catalog.get("d1")
        assert d1 is not None
        assert d1.status == "ready"
        assert d1.summary == "D1-new"
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("").summary == "R-new"  # type: ignore[union-attr]
        assert _user_text(provider.requests[0]) == (
            "- name: f1.md | type: file | summary: A2"
        )
        assert _user_text(provider.requests[1]) == (
            "- name: d1 | type: directory | summary: D1-new"
        )
        await catalog.close()

    asyncio.run(scenario())


def test_empty_directory_is_ready_and_parent_summary_converges(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "empty").mkdir()

    provider = ScriptedModelProvider(
        script=((_completion("Root with an empty directory."),),)
    )

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(provider)
        await _drain(catalog)

        empty = catalog.get("empty")
        root = catalog.get("")
        assert empty is not None
        assert root is not None
        assert empty.status == "ready"
        assert empty.summary == "Empty directory."
        assert root.status == "ready"
        assert root.summary == "Root with an empty directory."
        assert len(provider.requests) == 1
        assert _user_text(provider.requests[0]) == (
            "- name: empty | type: directory | summary: Empty directory."
        )
        await catalog.close()

    asyncio.run(scenario())


def test_failed_stale_directory_is_unavailable_to_its_parent(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "d1").mkdir()
    (repertoire / "library" / "d1" / "f1.md").write_text(
        "content\n",
        encoding="utf-8",
    )

    cache = _cache(tmp_path)
    cache.upsert(_fingerprint_of("content\n"), "file", "A1")
    cache.upsert(
        _directory_fingerprint((("f1.md", "file", "A1"),)),
        "directory",
        "D1-old",
    )
    cache.upsert(
        _directory_fingerprint((("d1", "directory", "D1-old"),)),
        "directory",
        "R-old",
    )
    cache.close()
    provider = _FailOnCallProvider(1, RuntimeError("directory failed"))

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(provider)

        file_entry = catalog.get("d1/f1.md")
        assert file_entry is not None
        catalog._entries["d1/f1.md"] = replace(file_entry, summary="A2")
        await catalog._cascade("d1/f1.md")
        await _drain(catalog)

        directory = catalog.get("d1")
        root = catalog.get("")
        assert directory is not None
        assert root is not None
        assert directory.status == "failed"
        assert directory.summary == "D1-old"
        assert root.status == "ready"
        assert _user_text(provider.requests[1]) == (
            "- name: d1 | type: directory | summary: unavailable"
        )
        await catalog.close()

    asyncio.run(scenario())


def test_empty_ready_summary_remains_distinct_from_unavailable(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "empty.md").write_text(
        "content\n",
        encoding="utf-8",
    )
    provider = ScriptedModelProvider(
        script=(
            (_completion(""),),
            (_completion("Root summary."),),
        )
    )

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(provider)
        await _drain(catalog)

        file_entry = catalog.get("empty.md")
        assert file_entry is not None
        assert file_entry.status == "ready"
        assert file_entry.summary == ""
        assert _user_text(provider.requests[1]) == (
            "- name: empty.md | type: file | summary: "
        )
        await catalog.close()

    asyncio.run(scenario())


def test_directory_failure_only_affects_directory(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "d1").mkdir()
    (repertoire / "library" / "d1" / "f1.md").write_text("content\n", encoding="utf-8")
    diagnostics: list[RuntimeDiagnostic] = []
    provider = _FailOnCallProvider(2, ModelContextOverflowSignal("too big"))

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(provider, on_diagnostic=diagnostics.append)
        await _drain(catalog)

        assert catalog.get("d1/f1.md").status == "ready"  # type: ignore[union-attr]
        d1 = catalog.get("d1")
        assert d1 is not None
        assert d1.status == "failed"
        assert d1.error == "context overflow"
        assert diagnostics == [
            RuntimeDiagnostic(
                kind="library.summary_context_overflow",
                message="library directory summary failed: d1",
                detail={"path": "d1", "error": "context overflow"},
            )
        ]
        assert catalog.get("").status == "ready"  # type: ignore[union-attr]
        await catalog.close()

    asyncio.run(scenario())


def test_directory_summaries_reused_across_runtimes(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "d1").mkdir()
    (repertoire / "library" / "d1" / "f1.md").write_text("content\n", encoding="utf-8")

    async def scenario() -> None:
        first = ScriptedModelProvider(
            script=(
                (_completion("F1."),),
                (_completion("D1."),),
                (_completion("Root."),),
            )
        )
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(first)
        await _drain(catalog)
        await catalog.close()

        second = ScriptedModelProvider(script=())
        _prepare_workspace(tmp_path)
        view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        catalog = await _LibraryCatalog.reconcile(
            view,
            _LocalBackendWorkspace(tmp_path, {}, view).filesystem,
            _cache(tmp_path),
        )
        catalog.start(second)
        await _drain(catalog)

        assert catalog.get("d1").status == "ready"  # type: ignore[union-attr]
        assert second.requests == ()
        await catalog.close()

    asyncio.run(scenario())
