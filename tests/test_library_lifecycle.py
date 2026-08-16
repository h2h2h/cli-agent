"""End-to-end proof of the model-generated Library index lifecycle.

Every scenario drives the public ``AgentRuntime`` with a scripted
provider, proving that Runtime open never waits for summaries, that the
worker converges files bottom up through directories, that the SQLite cache
replaces requests across restarts and Workspaces, and that failures,
cancellation, and external changes stay entry scoped.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from interaction_fakes import _ScriptedInteraction

from cli_agent.presets import open_default_runtime
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    CallbackEventSink,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    RuntimeDiagnostic,
    TextBlock,
    ToolCall,
    ToolCallReady,
    UserMessage,
)
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime.model import ModelContextOverflowSignal

_CONTEXT_POLICY = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)

_FILE_PREFIX = "The file content is:\n\n"


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def _library(workspace: Path) -> Path:
    return workspace / ".workspace" / "library"


def _index(workspace: Path, *parts: str) -> str:
    return (_library(workspace) / Path(*parts) / "index.md").read_text(encoding="utf-8")


def _completion(text: str) -> ModelCompletion:
    return ModelCompletion(message=AssistantMessage.text(text), finish_reason="stop")


def _catalog(runtime: AgentRuntime) -> _LibraryCatalog:
    return runtime._resources.capabilities.snapshot.library


async def _drain(catalog: _LibraryCatalog) -> None:
    await catalog._queue.join()  # type: ignore[union-attr]


async def _collect(
    runtime: AgentRuntime,
    text: str = "Hello",
) -> tuple[ModelEvent, ...]:
    if runtime._binding is None:
        await runtime.new_session()
    return tuple(
        [event async for event in runtime.run_turn(UserMessage.text(text))]
    )


class _LifecycleProvider:
    """Serve normal turns and scripted library summary requests.

    Summary requests carry no tools; every other request is a normal Agent
    turn. File summaries are mapped by exact source content; directory
    summaries are consumed positionally in the worker's bottom-up order.
    """

    def __init__(
        self,
        dir_summaries: tuple[str, ...] = (),
        *,
        by_content: dict[str, str] | None = None,
        gate: asyncio.Event | None = None,
        fail_on: int | None = None,
        overflow_on: int | None = None,
        tool_call_at: int | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
        on_turn: Callable[[], None] | None = None,
    ) -> None:
        self._dir_summaries = dir_summaries
        self._by_content = by_content or {}
        self._gate = gate
        self._fail_on = fail_on
        self._overflow_on = overflow_on
        self._tool_call_at = tool_call_at
        self._tool_calls_pending = list(tool_calls)
        self.on_turn = on_turn
        self.summary_calls = 0
        self.turn_calls = 0
        self._dir_calls = 0
        self.summary_texts: list[str] = []
        self.observe = False
        self._catalog: _LibraryCatalog | None = None

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.tools != ():
            self.turn_calls += 1
            if self.on_turn is not None and self.observe:
                self.on_turn()
            if self.turn_calls == self._tool_call_at:
                call = self._tool_calls_pending.pop(0)
                yield ToolCallReady(call=call)
                yield ModelCompletion(
                    message=AssistantMessage(content=(call,)),
                    finish_reason="tool_calls",
                )
                return
            yield _completion("Turn complete.")
            return

        self.summary_calls += 1
        index = self.summary_calls
        if self._gate is not None:
            await self._gate.wait()
        if index == self._fail_on:
            raise RuntimeError("boom")
        if index == self._overflow_on:
            raise ModelContextOverflowSignal("context overflow")
        block = request.messages[1].content[0]
        if not isinstance(block, TextBlock):
            raise AssertionError("summary request user content is not text")
        text = block.text
        self.summary_texts.append(text)
        if text.startswith(_FILE_PREFIX):
            yield _completion(
                self._by_content.get(
                    text[len(_FILE_PREFIX) :],
                    f"Summary {index}.",
                )
            )
            return
        self._dir_calls += 1
        if self._dir_calls <= len(self._dir_summaries):
            yield _completion(self._dir_summaries[self._dir_calls - 1])
            return
        yield _completion(f"Summary {index}.")


def test_runtime_open_never_waits_and_indexes_converge_bottom_up(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "notes").mkdir(parents=True)
    (repertoire / "library" / "notes" / "guide.md").write_text(
        "Guide content.\n", encoding="utf-8"
    )
    (repertoire / "library" / "top.txt").write_text("Top notes.\n", encoding="utf-8")

    gate = asyncio.Event()
    provider = _LifecycleProvider(
        ("Notes directory.", "Library root."),
        by_content={"Guide content.\n": "Guide.", "Top notes.\n": "Top."},
        gate=gate,
    )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=provider,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            events = await _collect(runtime)
            assert provider.turn_calls == 1
            assert events[-1] == _completion("Turn complete.")

            catalog = _catalog(runtime)
            assert catalog.get("notes/guide.md").status == "pending"  # type: ignore[union-attr]
            assert catalog.get("notes").status == "pending"  # type: ignore[union-attr]
            assert catalog.get("top.txt").status == "pending"  # type: ignore[union-attr]
            assert catalog.get("").status == "pending"  # type: ignore[union-attr]
            assert all(
                catalog.get(path).status == "pending"  # type: ignore[union-attr]
                for path in ("notes/guide.md", "top.txt", "notes", "")
            )
            assert "Summary generation pending." in _index(tmp_path)
            assert "Directory summary generation pending." in _index(tmp_path, "notes")

            gate.set()
            await _drain(catalog)

            assert catalog.get("notes/guide.md").status == "ready"  # type: ignore[union-attr]
            assert catalog.get("top.txt").status == "ready"  # type: ignore[union-attr]
            assert catalog.get("notes").status == "ready"  # type: ignore[union-attr]
            assert catalog.get("").status == "ready"  # type: ignore[union-attr]
            assert "Guide." in _index(tmp_path, "notes")
            assert "Notes directory." in _index(tmp_path)

            file_requests = [
                text for text in provider.summary_texts if text.startswith(_FILE_PREFIX)
            ]
            directory_requests = [
                text
                for text in provider.summary_texts
                if not text.startswith(_FILE_PREFIX)
            ]
            assert len(file_requests) == 2
            assert len(directory_requests) == 2
            assert "guide.md" in directory_requests[0]
            assert "top.txt" in directory_requests[1]
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_restart_reuses_sqlite_cache_without_repeat_requests(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "guide.md").write_text("Content.\n", encoding="utf-8")

    async def scenario() -> None:
        first = _LifecycleProvider(
            ("Library root.",),
            by_content={"Content.\n": "Guide summary."},
        )
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=first,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        await _collect(runtime)
        await _drain(_catalog(runtime))
        await runtime.close()

        second = _LifecycleProvider()
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=second,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            await _collect(runtime)
            catalog = _catalog(runtime)
            assert catalog.get("guide.md").status == "ready"  # type: ignore[union-attr]
            assert catalog.get("guide.md").summary == "Guide summary."  # type: ignore[union-attr]
            assert catalog.get("").status == "ready"  # type: ignore[union-attr]
            assert second.summary_calls == 0
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_runtime_files_write_is_stale_at_next_request_then_converges(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "notes").mkdir(parents=True)
    (repertoire / "library" / "notes" / "guide.md").write_text(
        "one\n", encoding="utf-8"
    )

    gate = asyncio.Event()
    observed: list[str] = []
    provider = _LifecycleProvider(
        ("Notes one.", "Root one.", "Notes two.", "Root two."),
        by_content={"one\n": "Guide one.", "two\n": "Guide two."},
        gate=gate,
        tool_call_at=2,
        tool_calls=(
            ToolCall(
                call_id="call_1",
                name="exec",
                arguments={
                    "command": "files write .workspace/library/notes/guide.md",
                    "stdin": "two\n",
                },
            ),
        ),
        on_turn=lambda: observed.append(
            provider._catalog.get("notes/guide.md").status  # type: ignore[union-attr]
        ),
    )
    gate.set()

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=provider,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            catalog = _catalog(runtime)
            provider._catalog = catalog
            await _collect(runtime)
            await _drain(catalog)
            assert catalog.get("notes/guide.md").status == "ready"  # type: ignore[union-attr]
            provider.observe = True

            gate.clear()
            await _collect(runtime, text="Rewrite guide")
            await _collect(runtime, text="What changed?")

            assert observed == ["ready", "stale", "stale"]
            entry = catalog.get("notes/guide.md")
            assert entry is not None
            assert entry.status == "stale"
            assert entry.summary == "Guide one."
            assert "Guide one." in _index(tmp_path, "notes")

            gate.set()
            await _drain(catalog)
            entry = catalog.get("notes/guide.md")
            assert entry is not None
            assert entry.status == "ready"
            assert entry.summary == "Guide two."
            assert "Guide two." in _index(tmp_path, "notes")
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_failure_and_overflow_stay_entry_scoped_then_restart_recovers(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "bad.md").write_text("boom content\n", encoding="utf-8")
    (repertoire / "library" / "good.md").write_text("good content\n", encoding="utf-8")

    diagnostics: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        first = _LifecycleProvider(
            ("Root with failed entries.",),
            fail_on=1,
            overflow_on=2,
        )
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=first,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
            events=CallbackEventSink(diagnostics.append),
        )
        try:
            await _collect(runtime)
            await _drain(_catalog(runtime))

            catalog = _catalog(runtime)
            assert catalog.get("bad.md").status == "failed"  # type: ignore[union-attr]
            assert catalog.get("bad.md").error == "boom"  # type: ignore[union-attr]
            assert catalog.get("good.md").status == "failed"  # type: ignore[union-attr]
            assert catalog.get("good.md").error == "context overflow"  # type: ignore[union-attr]
            assert {diagnostic.kind for diagnostic in diagnostics} == {
                "library.summary_failed",
                "library.summary_context_overflow",
            }
        finally:
            await runtime.close()

        second = _LifecycleProvider(
            ("Root recovered.",),
            by_content={
                "boom content\n": "Bad recovered.",
                "good content\n": "Good recovered.",
            },
        )
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=second,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            await _collect(runtime)
            await _drain(_catalog(runtime))
            catalog = _catalog(runtime)
            assert catalog.get("bad.md").status == "ready"  # type: ignore[union-attr]
            assert catalog.get("bad.md").summary == "Bad recovered."  # type: ignore[union-attr]
            assert catalog.get("good.md").status == "ready"  # type: ignore[union-attr]
            assert catalog.get("good.md").summary == "Good recovered."  # type: ignore[union-attr]
            assert catalog.get("").status == "ready"  # type: ignore[union-attr]
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_close_cancels_pending_summaries_and_restart_converges(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "slow.md").write_text("slow\n", encoding="utf-8")

    gate = asyncio.Event()

    async def scenario() -> None:
        blocking = _LifecycleProvider(gate=gate)
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=blocking,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        await _collect(runtime)
        await runtime.close()

        fresh = _LifecycleProvider(
            ("Library root.",),
            by_content={"slow\n": "Slow summary."},
        )
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=fresh,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            await _collect(runtime)
            await _drain(_catalog(runtime))
            assert _catalog(runtime).get("slow.md").status == "ready"  # type: ignore[union-attr]
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_capability_view_scenarios_index_the_effective_library(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "index.md").write_text(
        "LOWER ROOT INDEX, NOT A SOURCE\n", encoding="utf-8"
    )
    (repertoire / "library" / "resources").mkdir()
    (repertoire / "library" / "resources" / "design.md").write_text(
        "# Design\n", encoding="utf-8"
    )
    (repertoire / "library" / "memory").mkdir()
    (repertoire / "library" / "memory" / "notes.txt").write_text(
        "Note.\n", encoding="utf-8"
    )
    (repertoire / "library" / "override.md").write_text("lower\n", encoding="utf-8")
    (repertoire / "library" / "hidden.md").write_text("secret\n", encoding="utf-8")
    gate = asyncio.Event()

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
                provider=_LifecycleProvider(
                    ("Resources.", "Memory.", "Library root."),
                by_content={
                    "# Design\n": "Design.",
                    "Note.\n": "Note.",
                        "upper\n": "Override.",
                    },
                    gate=gate,
            ),
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            catalog = _catalog(runtime)
            view = runtime._resources.capabilities.overlay.view
            view._copy_up(Path(view.root) / "library" / "override.md")
            (Path(view.root) / "library" / "override.md").write_text(
                "upper\n", encoding="utf-8"
            )
            whiteout = view._whiteouts / "library" / "hidden.md"
            whiteout.parent.mkdir(parents=True, exist_ok=True)
            whiteout.write_text("", encoding="utf-8")
            (Path(view.root) / "library" / "hidden.md").unlink()

            await _collect(runtime)
            gate.set()
            await _drain(catalog)

            by_path = {entry.path: entry for entry in catalog.entries}
            assert set(by_path) == {
                "resources",
                "resources/design.md",
                "memory",
                "memory/notes.txt",
                "override.md",
            }
            assert catalog.get("index.md") is None
            assert catalog.get("hidden.md") is None
            assert by_path["override.md"].provenance == "workspace"
            assert by_path["override.md"].shadows_repertoire is True
            assert by_path["override.md"].summary == "Override."
            assert by_path["resources/design.md"].summary == "Design."
            assert by_path["memory/notes.txt"].summary == "Note."
            assert catalog.get("").summary == "Library root."  # type: ignore[union-attr]
            assert "LOWER ROOT INDEX" not in _index(tmp_path)
            assert (
                repertoire / "library" / "index.md"
            ).read_text() == "LOWER ROOT INDEX, NOT A SOURCE\n"
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_external_changes_observed_at_request_boundaries_and_converge(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "guide.md").write_text("one\n", encoding="utf-8")

    gate = asyncio.Event()
    observed: list[tuple[str, str]] = []
    provider = _LifecycleProvider(
        ("Library root one.", "Library root two."),
        by_content={"one\n": "Guide one.", "two\n": "Guide two."},
        gate=gate,
        on_turn=lambda: observed.append(_observed_states(provider)),
    )
    gate.set()

    def _observed_states(provider: _LifecycleProvider) -> tuple[str, str]:
        catalog = provider._catalog
        external = catalog.get("external.md")  # type: ignore[union-attr]
        return (
            catalog.get("guide.md").status,  # type: ignore[union-attr]
            "absent" if external is None else external.status,
        )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=provider,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            catalog = _catalog(runtime)
            provider._catalog = catalog
            await _collect(runtime)
            await _drain(catalog)
            assert catalog.get("guide.md").status == "ready"  # type: ignore[union-attr]
            provider.observe = True

            gate.clear()
            (_library(tmp_path) / "external.md").write_text("new\n", encoding="utf-8")
            await _collect(runtime, text="What is new?")
            assert observed[-1] == ("ready", "pending")

            (repertoire / "library" / "guide.md").write_text("two\n", encoding="utf-8")
            await _collect(runtime, text="Did guide change?")
            assert observed[-1] == ("stale", "pending")

            (_library(tmp_path) / "external.md").unlink()
            await _collect(runtime, text="Anything removed?")
            assert observed[-1] == ("stale", "absent")

            gate.set()
            await _drain(catalog)
            assert catalog.get("guide.md").summary == "Guide two."  # type: ignore[union-attr]
            assert catalog.get("external.md") is None
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_file_summaries_reuse_across_workspaces(tmp_path: Path) -> None:
    source = "shared content\n"

    async def scenario() -> None:
        first_workspace = tmp_path / "first"
        first_workspace.mkdir()
        first = _repertoire(first_workspace)
        (first / "library" / "guide.md").write_text(source, encoding="utf-8")
        first_provider = _LifecycleProvider(
            ("First root.",),
            by_content={source: "Shared guide."},
        )
        runtime = await open_default_runtime(
            workspace=first_workspace,
            repertoire=first,
            provider=first_provider,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        await _collect(runtime)
        await _drain(_catalog(runtime))
        await runtime.close()

        second_workspace = tmp_path / "second"
        second_workspace.mkdir()
        second = _repertoire(second_workspace)
        (second / "library" / "docs.md").write_text(source, encoding="utf-8")
        second_provider = _LifecycleProvider(
            ("Second root.",),
        )
        runtime = await open_default_runtime(
            workspace=second_workspace,
            repertoire=second,
            provider=second_provider,
            interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        try:
            await _collect(runtime)
            await _drain(_catalog(runtime))
            entry = _catalog(runtime).get("docs.md")
            assert entry is not None
            assert entry.status == "ready"
            assert entry.summary == "Shared guide."
            assert second_provider.summary_calls == 1
        finally:
            await runtime.close()

    asyncio.run(scenario())
