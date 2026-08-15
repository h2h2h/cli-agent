"""ContextEngine contract: hydration, rebuild, isolation, snapshot commit."""

import asyncio
from pathlib import Path

import pytest

from cli_agent.errors.session import SessionPersistenceError
from cli_agent.runtime import (
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelUsage,
    ScriptedModelProvider,
    SessionUsage,
    SystemMessage,
    TextDelta,
    UserMessage,
)
from cli_agent.runtime._context.engine import (
    CONTEXT_DERIVATION_VERSION,
    ContextEngineFactory,
    _ContextEngine,
)
from cli_agent.runtime._context.summarizer import (
    SUMMARY_DELIMITER_CLOSE,
    SUMMARY_DELIMITER_OPEN,
)
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._session import ContextSnapshot, SessionConfig

SYSTEM_MESSAGE = SystemMessage.text("System")
SESSION_ID = "engine-session"
SUMMARY_TEXT = (
    "## Progress\nchecked the workspace\n"
    "## Files\nconfig.py edited\n"
    "## Todo\nrun the tests\n"
    "## Context\nuser prefers concise output"
)


def _policy(budget: int = 45_000) -> ContextPolicy:
    return ContextPolicy(
        context_window_tokens=budget + 5_000,
        output_reserve_tokens=5_000,
        safety_margin_tokens=0,
        minimum_reclaim_tokens=1,
    )


def _engine(
    *,
    provider: object = None,
    policy: ContextPolicy | None = None,
    commit_snapshot=None,
) -> _ContextEngine:
    engine = _ContextEngine(
        session_id=SESSION_ID,
        context_policy=policy if policy is not None else _policy(),
        provider=provider if provider is not None else ScriptedModelProvider(script=()),
        commit_snapshot=commit_snapshot,
    )
    engine.hydrate(system_message=SYSTEM_MESSAGE, snapshot=None, journal=(), revision=0)
    return engine


def _open_store(path: Path) -> tuple[_StateDatabase, SessionStore]:
    database = _StateDatabase.open(path)
    return database, SessionStore(database)


def _usage(*, input_tokens: int, output_tokens: int) -> ModelUsage:
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _summary_completion() -> ModelCompletion:
    return ModelCompletion(
        message=AssistantMessage.text(SUMMARY_TEXT),
        finish_reason="stop",
        usage=_usage(input_tokens=1_000, output_tokens=200),
    )


def _long_turn(engine: _ContextEngine, *, text: str, length: int) -> None:
    engine.apply(UserMessage.text(text), engine.revision + 1)
    engine.apply(AssistantMessage.text("x" * length), engine.revision + 1)


def test_hydrate_without_snapshot_equals_raw_journal() -> None:
    journal = (
        UserMessage.text("one"),
        AssistantMessage.text("answer one"),
        UserMessage.text("two"),
        AssistantMessage.text("answer two"),
    )
    engine = _engine()
    engine.hydrate(
        system_message=SYSTEM_MESSAGE,
        snapshot=None,
        journal=journal,
        revision=4,
    )

    prepared = asyncio.run(engine.prepare())

    assert prepared.revision == 4
    assert prepared.request.messages == (SYSTEM_MESSAGE, *journal)
    assert engine._ledger.summary is None


def test_hydrate_with_snapshot_applies_only_journal_delta() -> None:
    user_one = UserMessage.text("one")
    assistant_one = AssistantMessage.text("answer one")
    user_two = UserMessage.text("two")
    assistant_two = AssistantMessage.text("answer two")
    snapshot = ContextSnapshot(
        session_id=SESSION_ID,
        source_revision=2,
        summary=None,
        context=(user_one, assistant_one),
        derivation_version=CONTEXT_DERIVATION_VERSION,
    )
    engine = _engine()
    engine.hydrate(
        system_message=SYSTEM_MESSAGE,
        snapshot=snapshot,
        journal=(user_one, assistant_one, user_two, assistant_two),
        revision=4,
    )

    prepared = asyncio.run(engine.prepare())

    assert prepared.revision == 4
    assert prepared.request.messages == (
        SYSTEM_MESSAGE,
        user_one,
        assistant_one,
        user_two,
        assistant_two,
    )


def test_hydrate_replaces_the_current_system_message() -> None:
    replacement = SystemMessage.text("Replacement instruction")
    engine = _engine()
    engine.hydrate(
        system_message=SYSTEM_MESSAGE,
        snapshot=None,
        journal=(UserMessage.text("one"),),
        revision=1,
    )

    first = asyncio.run(engine.prepare())
    engine.hydrate(
        system_message=replacement,
        snapshot=None,
        journal=(UserMessage.text("one"),),
        revision=1,
    )
    second = asyncio.run(engine.prepare())

    assert first.request.messages[0] is SYSTEM_MESSAGE
    assert second.request.messages[0] is replacement
    assert second.request.messages[1:] == first.request.messages[1:]


def test_engine_instances_do_not_share_projection_state() -> None:
    first = _engine()
    second = _engine()
    first.apply(UserMessage.text("first session"), first.revision + 1)
    second.apply(UserMessage.text("second session"), second.revision + 1)

    first_prepared = asyncio.run(first.prepare())
    second_prepared = asyncio.run(second.prepare())

    assert first_prepared.request.messages[-1] == UserMessage.text("first session")
    assert second_prepared.request.messages[-1] == UserMessage.text("second session")
    assert first.usage == second.usage == SessionUsage(input_tokens=0, output_tokens=0)


def test_snapshot_commit_delegates_proposal_and_usage_before_projection() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    proposals: list[tuple[object, object, object]] = []
    engine = _engine(
        provider=provider,
        policy=_policy(40_000),
        commit_snapshot=lambda snapshot, usage, revision: proposals.append(
            (snapshot, usage, revision)
        ),
    )
    _long_turn(engine, text="one", length=80_000)
    _long_turn(engine, text="two", length=80_000)
    _long_turn(engine, text="three", length=80_000)

    prepared = asyncio.run(engine.prepare())

    assert len(proposals) == 1
    snapshot, usage_record, expected_revision = proposals[0]
    assert snapshot.session_id == SESSION_ID
    assert snapshot.source_revision == 6
    assert expected_revision == 6
    assert snapshot.summary == SUMMARY_TEXT
    assert snapshot.derivation_version == CONTEXT_DERIVATION_VERSION
    assert snapshot.context[0].content[0].text.startswith(SUMMARY_DELIMITER_OPEN)
    assert snapshot.context[0].content[0].text.endswith(SUMMARY_DELIMITER_CLOSE)
    assert snapshot.context[1:] == (
        UserMessage.text("three"),
        AssistantMessage.text("x" * 80_000),
    )
    assert usage_record.purpose == "compaction"
    assert usage_record.input_tokens == 1_000
    assert usage_record.output_tokens == 200
    assert prepared.operations[0].tier == 3
    assert engine._ledger.summary == SUMMARY_TEXT
    provider.assert_exhausted()


def test_failed_snapshot_commit_leaves_projection_unchanged() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    engine = _engine(
        provider=provider,
        policy=_policy(40_000),
        commit_snapshot=lambda snapshot, usage, revision: (_ for _ in ()).throw(
            SessionPersistenceError(operation="save_snapshot", session_id=SESSION_ID)
        ),
    )
    _long_turn(engine, text="one", length=80_000)
    _long_turn(engine, text="two", length=80_000)
    _long_turn(engine, text="three", length=80_000)

    with pytest.raises(SessionPersistenceError):
        asyncio.run(engine.prepare())

    assert engine._ledger.summary is None
    assert engine.history[1] == UserMessage.text("one")
    provider.assert_exhausted()


def test_factory_hydrates_from_store_and_commits_durable_snapshot(
    tmp_path: Path,
) -> None:
    database, store = _open_store(tmp_path / "state.sqlite3")
    store.create(SESSION_ID, "/workspace", SessionConfig(system_prompt="{}"))
    revision = 0
    for text, length in (("one", 80_000), ("two", 80_000), ("three", 80_000)):
        revision = store.append(
            SESSION_ID,
            UserMessage.text(text),
            expected_revision=revision,
        )
        revision = store.append(
            SESSION_ID,
            AssistantMessage.text("x" * length),
            expected_revision=revision,
        )
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    factory = ContextEngineFactory(
        store=store,
        context_policy=_policy(40_000),
    )
    engine = factory.create(
        SESSION_ID,
        provider=provider,
        system_message=SYSTEM_MESSAGE,
    )

    prepared = asyncio.run(engine.prepare())

    assert prepared.operations[0].tier == 3
    stored = store.load_snapshot(
        SESSION_ID,
        derivation_version=CONTEXT_DERIVATION_VERSION,
    )
    assert stored is not None
    assert stored.summary == SUMMARY_TEXT
    assert stored.source_revision == 6
    assert stored.context[0].content[0].text.startswith(SUMMARY_DELIMITER_OPEN)
    assert stored.context[1:] == (
        UserMessage.text("three"),
        AssistantMessage.text("x" * 80_000),
    )
    input_tokens, output_tokens = store.usage_total(SESSION_ID)
    assert (input_tokens, output_tokens) == (1_000, 200)

    resumed = factory.create(
        SESSION_ID,
        provider=ScriptedModelProvider(script=()),
        system_message=SYSTEM_MESSAGE,
    )
    resumed_request = asyncio.run(resumed.prepare()).request
    assert resumed_request.messages[0] is SYSTEM_MESSAGE
    assert (
        resumed_request.messages[1].content[0].text.startswith(SUMMARY_DELIMITER_OPEN)
    )
    assert resumed_request.messages[2:] == (
        UserMessage.text("three"),
        AssistantMessage.text("x" * 80_000),
    )
    provider.assert_exhausted()


def test_factory_rebuilds_when_snapshot_payload_is_corrupt(tmp_path: Path) -> None:
    database, store = _open_store(tmp_path / "state.sqlite3")
    store.create(SESSION_ID, "/workspace", SessionConfig(system_prompt="{}"))
    revision = 0
    for text in ("one", "two"):
        revision = store.append(
            SESSION_ID,
            UserMessage.text(text),
            expected_revision=revision,
        )
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO session_context_snapshots "
            "(session_id, source_revision, derivation_version, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                SESSION_ID,
                revision,
                CONTEXT_DERIVATION_VERSION,
                "{broken",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    factory = ContextEngineFactory(store=store, context_policy=_policy())

    engine = factory.create(
        SESSION_ID,
        provider=ScriptedModelProvider(script=()),
        system_message=SYSTEM_MESSAGE,
    )

    prepared = asyncio.run(engine.prepare())
    assert prepared.request.messages == (
        SYSTEM_MESSAGE,
        UserMessage.text("one"),
        UserMessage.text("two"),
    )
    assert engine._ledger.summary is None


def test_factory_rebuilds_when_derivation_version_mismatches(tmp_path: Path) -> None:
    database, store = _open_store(tmp_path / "state.sqlite3")
    store.create(SESSION_ID, "/workspace", SessionConfig(system_prompt="{}"))
    revision = store.append(
        SESSION_ID,
        UserMessage.text("one"),
        expected_revision=0,
    )
    snapshot = ContextSnapshot(
        session_id=SESSION_ID,
        source_revision=revision,
        summary=None,
        context=(UserMessage.text("one"),),
        derivation_version="stale-derivation",
    )
    store.save_snapshot(snapshot, expected_revision=revision)
    factory = ContextEngineFactory(store=store, context_policy=_policy())

    engine = factory.create(
        SESSION_ID,
        provider=ScriptedModelProvider(script=()),
        system_message=SYSTEM_MESSAGE,
    )

    prepared = asyncio.run(engine.prepare())
    assert prepared.request.messages == (SYSTEM_MESSAGE, UserMessage.text("one"))
    assert engine._ledger.summary is None


def test_replacement_binding_hydrates_fresh_engine_from_store(tmp_path: Path) -> None:
    database, store = _open_store(tmp_path / "state.sqlite3")
    store.create(SESSION_ID, "/workspace", SessionConfig(system_prompt="{}"))
    revision = store.append(
        SESSION_ID,
        UserMessage.text("one"),
        expected_revision=0,
    )
    factory = ContextEngineFactory(store=store, context_policy=_policy())
    first = factory.create(
        SESSION_ID,
        provider=ScriptedModelProvider(script=()),
        system_message=SYSTEM_MESSAGE,
    )
    revision = store.append(
        SESSION_ID,
        UserMessage.text("two"),
        expected_revision=revision,
    )

    replacement = factory.create(
        SESSION_ID,
        provider=ScriptedModelProvider(script=()),
        system_message=SYSTEM_MESSAGE,
    )

    replacement_request = asyncio.run(replacement.prepare()).request
    assert replacement_request.messages == (
        SYSTEM_MESSAGE,
        UserMessage.text("one"),
        UserMessage.text("two"),
    )
    first_request = asyncio.run(first.prepare()).request
    assert first_request.messages == (SYSTEM_MESSAGE, UserMessage.text("one"))
    assert replacement.usage.input_tokens == 0
