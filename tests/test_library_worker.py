import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelContextOverflowError,
    ModelEvent,
    ModelRequest,
    RuntimeDiagnostic,
    ScriptedModelProvider,
    SystemMessage,
    TextBlock,
    UserMessage,
)
from cli_agent.runtime._capability.library.cache import _SummaryCache
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.library.facts import (
    _content_digest,
    _file_fingerprint,
)
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._state_db import _StateDatabase

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


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


def _index(workspace: Path) -> str:
    return (workspace / ".workspace" / "library" / "index.md").read_text(
        encoding="utf-8"
    )


class _RaisingProvider:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request
        raise self._error
        yield


class _BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request
        self.started.set()
        await asyncio.Event().wait()
        yield


def test_worker_generates_file_summaries_and_refreshes_indexes(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "first.md").write_text(
        "first content\n", encoding="utf-8"
    )
    (repertoire / "library" / "second.txt").write_text(
        "second content\n", encoding="utf-8"
    )
    provider = ScriptedModelProvider(
        script=(
            (_completion("Summary of first."),),
            (_completion("Summary of second."),),
        )
    )

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, repertoire)
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))
        assert catalog.get("first.md").status == "pending"  # type: ignore[union-attr]

        catalog.start(provider)
        await catalog._queue.join()  # type: ignore[union-attr]

        assert catalog.get("first.md").status == "ready"  # type: ignore[union-attr]
        assert catalog.get("first.md").summary == "Summary of first."  # type: ignore[union-attr]
        assert catalog.get("second.txt").status == "ready"  # type: ignore[union-attr]

        index = _index(tmp_path)
        assert (
            "| first.md | ready | repertoire | no | Summary of first. | [first.md](./first.md) |"
            in index
        )
        assert (
            "| second.txt | ready | repertoire | no | Summary of second. | [second.txt](./second.txt) |"
            in index
        )

        requests = provider.requests
        assert len(requests) == 2
        for request in requests:
            assert request.tools == ()
            system, user = request.messages
            assert isinstance(system, SystemMessage)
            assert "untrusted data" in system.content[0].text
            assert user.content[0].text in {
                "The file content is:\n\nfirst content\n",
                "The file content is:\n\nsecond content\n",
            }
            assert "first.md" not in user.content[0].text
            assert "second.txt" not in user.content[0].text

        await catalog.close()

    asyncio.run(scenario())


def test_one_summary_updates_every_file_with_the_same_fingerprint(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    shared_content = "shared content\n"
    (repertoire / "library" / "first.md").write_text(
        shared_content,
        encoding="utf-8",
    )
    (repertoire / "library" / "second.txt").write_text(
        shared_content,
        encoding="utf-8",
    )
    provider = ScriptedModelProvider(
        script=((_completion("Shared summary."),),),
    )

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, repertoire)
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))

        catalog.start(provider)
        await catalog._queue.join()  # type: ignore[union-attr]

        first = catalog.get("first.md")
        second = catalog.get("second.txt")
        assert first is not None
        assert second is not None
        assert first.status == "ready"
        assert second.status == "ready"
        assert first.summary == "Shared summary."
        assert second.summary == "Shared summary."
        assert len(provider.requests) == 1
        index = _index(tmp_path)
        assert "| first.md | ready |" in index
        assert "| second.txt | ready |" in index

        await catalog.close()

    asyncio.run(scenario())


def test_cache_hit_never_calls_the_model(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "cached.md").write_text("content\n", encoding="utf-8")

    cache = _cache(tmp_path)
    cache.upsert(_fingerprint_of("content\n"), "file", "Cached summary.")
    cache.close()

    provider = ScriptedModelProvider(script=())

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, repertoire)
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))
        assert catalog.get("cached.md").status == "ready"  # type: ignore[union-attr]

        catalog.start(provider)
        await catalog._queue.join()  # type: ignore[union-attr]

        assert provider.requests == ()
        await catalog.close()

    asyncio.run(scenario())


def test_provider_failure_marks_entry_failed_with_diagnostic(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "broken.md").write_text("content\n", encoding="utf-8")
    diagnostics: list[RuntimeDiagnostic] = []
    provider = _RaisingProvider(RuntimeError("provider exploded"))

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, repertoire)
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))
        catalog.start(provider, on_diagnostic=diagnostics.append)
        await catalog._queue.join()  # type: ignore[union-attr]

        entry = catalog.get("broken.md")
        assert entry is not None
        assert entry.status == "failed"
        assert entry.error == "provider exploded"
        assert diagnostics == [
            RuntimeDiagnostic(
                kind="library.summary_failed",
                message="library file summary failed: broken.md",
                detail={"path": "broken.md", "error": "provider exploded"},
            )
        ]
        assert "| broken.md | failed | repertoire | no | provider exploded |" in _index(
            tmp_path
        )
        await catalog.close()

    asyncio.run(scenario())


def test_context_overflow_marks_entry_failed_with_specific_diagnostic(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "big.md").write_text("content\n", encoding="utf-8")
    diagnostics: list[RuntimeDiagnostic] = []
    provider = _RaisingProvider(ModelContextOverflowError("context window exceeded"))

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, repertoire)
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))
        catalog.start(provider, on_diagnostic=diagnostics.append)
        await catalog._queue.join()  # type: ignore[union-attr]

        entry = catalog.get("big.md")
        assert entry is not None
        assert entry.status == "failed"
        assert entry.error == "context overflow"
        assert diagnostics == [
            RuntimeDiagnostic(
                kind="library.summary_context_overflow",
                message="library file summary failed: big.md",
                detail={"path": "big.md", "error": "context overflow"},
            )
        ]
        await catalog.close()

    asyncio.run(scenario())


def test_failure_diagnostics_never_contain_source_content(tmp_path: Path) -> None:
    source = "TOP-SECRET-SOURCE-CONTENT"
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "secret.md").write_text(source, encoding="utf-8")
    diagnostics: list[RuntimeDiagnostic] = []
    provider = _RaisingProvider(RuntimeError("credentials rejected"))

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, repertoire)
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))
        catalog.start(provider, on_diagnostic=diagnostics.append)
        await catalog._queue.join()  # type: ignore[union-attr]

        assert catalog.get("secret.md").status == "failed"  # type: ignore[union-attr]
        for diagnostic in diagnostics:
            assert source not in diagnostic.message
            assert source not in str(diagnostic.detail)
        await catalog.close()

    asyncio.run(scenario())


def test_close_cancels_in_progress_worker(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "slow.md").write_text("content\n", encoding="utf-8")
    provider = _BlockingProvider()

    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, repertoire)
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))
        catalog.start(provider)
        await provider.started.wait()
        worker_task = catalog._worker_task
        assert worker_task is not None
        assert not worker_task.done()

        await catalog.close()

        assert worker_task.done()
        assert worker_task.cancelled()
        assert catalog.get("slow.md").status == "pending"  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_runtime_open_does_not_wait_for_summaries(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "doc.md").write_text("content\n", encoding="utf-8")
    provider = _BlockingProvider()

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )

        assert runtime._resources.library_catalog.get("doc.md").status == "pending"  # type: ignore[union-attr]
        assert (
            "| doc.md | pending | repertoire | no | Summary generation pending. |"
            in _index(tmp_path)
        )

        await runtime.close()

    asyncio.run(scenario())


def test_internal_summaries_stay_out_of_session_history(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "doc.md").write_text(
        "uniquely distinctive content\n", encoding="utf-8"
    )
    provider = ScriptedModelProvider(
        script=(
            (_completion("File summary."),),
            (_completion("Turn response."),),
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        await runtime._resources.library_catalog._queue.join()  # type: ignore[union-attr]
        events = tuple(
            [event async for event in runtime.run_turn("s", UserMessage.text("Hello"))]
        )
        assert len(events) == 1

        history = runtime._sessions["s"].loop.history
        session_text = "\n".join(
            block.text
            for message in history
            for block in message.content
            if isinstance(block, TextBlock)
        )
        assert "File summary." not in session_text
        assert "uniquely distinctive content" not in session_text

        await runtime.close()

    asyncio.run(scenario())


def test_close_without_start_still_closes_database(tmp_path: Path) -> None:
    async def scenario() -> None:
        _prepare_workspace(tmp_path)
        view = _CapabilityView.open(tmp_path, _repertoire(tmp_path))
        catalog = await _LibraryCatalog.reconcile(view, _cache(tmp_path))
        await catalog.close()

    asyncio.run(scenario())
