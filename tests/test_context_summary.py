import asyncio

from cli_agent.runtime import (
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ScriptedModelProvider,
    SystemMessage,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._context.engine import _ContextEngine
from cli_agent.runtime._context.summarizer import (
    SUMMARY_DELIMITER_CLOSE,
    SUMMARY_DELIMITER_OPEN,
)

SYSTEM_MESSAGE = SystemMessage.text("System")
SESSION_ID = "test-session"

SUMMARY_TEXT = (
    "## Progress\nchecked the workspace\n"
    "## Files\nconfig.py edited\n"
    "## Todo\nrun the tests\n"
    "## Context\nuser prefers concise output"
)


def _policy() -> ContextPolicy:
    return ContextPolicy(
        context_window_tokens=45_000,
        output_reserve_tokens=5_000,
        safety_margin_tokens=0,
        minimum_reclaim_tokens=1,
    )


def _failure_policy() -> ContextPolicy:
    return ContextPolicy(
        context_window_tokens=75_000,
        output_reserve_tokens=5_000,
        safety_margin_tokens=0,
        minimum_reclaim_tokens=1,
    )


def _engine(
    policy: ContextPolicy,
    provider: object,
) -> _ContextEngine:
    engine = _ContextEngine(
        session_id=SESSION_ID,
        context_policy=policy,
        provider=provider,  # type: ignore[arg-type]
    )
    engine.hydrate(system_message=SYSTEM_MESSAGE, snapshot=None, journal=(), revision=0)
    return engine


def _apply(engine: _ContextEngine, message: object) -> None:
    assert isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage))
    engine.apply(message, engine.revision + 1)


def _long_turn(manager: _ContextEngine, *, user_text: str, length: int) -> None:
    _apply(manager, UserMessage.text(user_text))
    _apply(manager, AssistantMessage.text("x" * length))


def _run(prepare_coroutine):
    return asyncio.run(prepare_coroutine)


def _summary_completion(text: str = SUMMARY_TEXT) -> ModelCompletion:
    return ModelCompletion(
        message=AssistantMessage.text(text),
        finish_reason="stop",
    )


def test_tier3_summarizes_old_turns_and_projects_assistant_data() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    manager = _engine(_policy(), provider)
    _long_turn(manager, user_text="one", length=80_000)
    _long_turn(manager, user_text="two", length=80_000)
    _long_turn(manager, user_text="three", length=80_000)

    prepared = _run(manager.prepare())

    assert len(provider.requests) == 1
    summary_request = provider.requests[0]
    assert summary_request.tools == ()
    assert isinstance(summary_request.messages[0], SystemMessage)
    assert "## Progress" in summary_request.messages[0].content[0].text
    assert summary_request.messages[1] == UserMessage.text(
        "New transcript to merge into the summary:"
    )
    history = manager.history
    assert len(history) == 4
    assert isinstance(history[0], SystemMessage)
    projected = history[1]
    assert isinstance(projected, AssistantMessage)
    projected_text = projected.content[0].text
    assert projected_text.startswith(SUMMARY_DELIMITER_OPEN)
    assert projected_text.endswith(SUMMARY_DELIMITER_CLOSE)
    for section in ("## Progress", "## Files", "## Todo", "## Context"):
        assert section in projected_text
    assert history[2:] == (
        UserMessage.text("three"),
        AssistantMessage.text("x" * 80_000),
    )
    assert manager._ledger.summary == SUMMARY_TEXT
    assert manager._ledger.summary_frontier == 2
    assert len(prepared.operations) == 1
    operation = prepared.operations[0]
    assert operation.tier == 3
    assert operation.entries_changed == 2
    assert operation.reason == "watermark"
    provider.assert_exhausted()


def test_tier3_merges_old_summary_with_new_delta() -> None:
    provider = ScriptedModelProvider(
        script=(
            (TextDelta(text="first"), _summary_completion()),
            (
                TextDelta(text="second"),
                _summary_completion(
                    SUMMARY_TEXT.replace("checked the workspace", "merged")
                ),
            ),
        )
    )
    manager = _engine(_policy(), provider)
    _long_turn(manager, user_text="one", length=80_000)
    _long_turn(manager, user_text="two", length=80_000)
    _long_turn(manager, user_text="three", length=80_000)

    _run(manager.prepare())
    _long_turn(manager, user_text="four", length=80_000)
    second = _run(manager.prepare())

    assert len(provider.requests) == 2
    second_request = provider.requests[1]
    assert second_request.messages[1] == UserMessage.text(
        f"Previous summary to extend:\n{SUMMARY_TEXT}"
    )
    assert UserMessage.text("three") in second_request.messages
    assert second_request.messages[2] == UserMessage.text(
        "New transcript to merge into the summary:"
    )
    history = manager.history
    assert history[1].content[0].text == (
        f"{SUMMARY_DELIMITER_OPEN}\n"
        f"{SUMMARY_TEXT.replace('checked the workspace', 'merged')}\n"
        f"{SUMMARY_DELIMITER_CLOSE}"
    )
    assert history[2:] == (
        UserMessage.text("four"),
        AssistantMessage.text("x" * 80_000),
    )
    assert manager._ledger.summary_frontier == 2
    assert len(second.operations) == 1
    provider.assert_exhausted()


def test_tier3_never_splits_a_parallel_tool_exchange() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    manager = _engine(_policy(), provider)
    first_call = ToolCall(call_id="call_a", name="exec", arguments={"command": "a"})
    second_call = ToolCall(call_id="call_b", name="exec", arguments={"command": "b"})
    _apply(manager, UserMessage.text("one"))
    _apply(manager, AssistantMessage(content=(first_call, second_call)))
    _apply(
        manager,
        ToolResultMessage(
            content=(
                ToolResult(call_id="call_a", output={"ok": True, "chunks": [1]}),
                ToolResult(call_id="call_b", output={"ok": True, "chunks": [1]}),
            )
        ),
    )
    _apply(manager, AssistantMessage.text("x" * 160_000))
    _long_turn(manager, user_text="two", length=80_000)

    _run(manager.prepare())

    summary_request = provider.requests[0]
    assert summary_request.messages[1] == UserMessage.text(
        "New transcript to merge into the summary:"
    )
    exchange = summary_request.messages[3:6]
    assert exchange[0] == AssistantMessage(content=(first_call, second_call))
    assert isinstance(exchange[1], ToolResultMessage)
    assert tuple(result.call_id for result in exchange[1].content) == (
        "call_a",
        "call_b",
    )
    assert isinstance(exchange[2], AssistantMessage)
    provider.assert_exhausted()


def test_tier3_keeps_the_active_turn_out_of_the_summary() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    manager = _engine(_policy(), provider)
    _long_turn(manager, user_text="one", length=80_000)
    _long_turn(manager, user_text="two", length=80_000)
    active_call = ToolCall(call_id="call_x", name="exec", arguments={"command": "x"})
    _apply(manager, UserMessage.text("three"))
    _apply(manager, AssistantMessage(content=(active_call,)))
    _apply(
        manager,
        ToolResultMessage(
            content=(ToolResult(call_id="call_x", output={"ok": True, "chunks": [1]}),)
        ),
    )

    _run(manager.prepare())

    summary_request = provider.requests[0]
    delta = summary_request.messages[2:]
    assert UserMessage.text("three") not in delta
    assert AssistantMessage(content=(active_call,)) not in delta
    history = manager.history
    assert len(history) == 7
    assert history[2] == UserMessage.text("two")
    assert history[3] == AssistantMessage.text("x" * 80_000)
    assert history[4] == UserMessage.text("three")
    assert history[5] == AssistantMessage(content=(active_call,))
    assert isinstance(history[6], ToolResultMessage)
    provider.assert_exhausted()


def test_tier3_does_not_run_when_deterministic_tiers_suffice() -> None:
    provider = ScriptedModelProvider(script=())
    manager = _engine(_policy(), provider)
    result = {
        "ok": True,
        "exec_id": "exec_1",
        "status": "exited",
        "exit_code": 0,
        "chunks": [
            {
                "cursor": i,
                "stream": "stdout",
                "text": "x" * 4_000,
                "timestamp": "2026-01-01T00:00:00Z",
            }
            for i in range(200)
        ],
        "next_cursor": 200,
        "is_terminal": True,
        "truncated": False,
        "available_from": 0,
    }
    old_call = ToolCall(call_id="call_old", name="exec", arguments={"command": "old"})
    _apply(manager, UserMessage.text("zero"))
    _apply(manager, AssistantMessage(content=(old_call,)))
    _apply(
        manager,
        ToolResultMessage(content=(ToolResult(call_id="call_old", output=result),)),
    )
    _apply(manager, AssistantMessage.text("done"))
    _long_turn(manager, user_text="two", length=80_000)

    prepared = _run(manager.prepare())

    assert provider.requests == ()
    assert prepared.operations
    assert manager._ledger.summary is None
    provider.assert_exhausted()


def test_tier3_skips_when_no_complete_turns_are_available() -> None:
    provider = ScriptedModelProvider(script=())
    manager = _engine(_policy(), provider)
    _long_turn(manager, user_text="one", length=152_000)

    prepared = _run(manager.prepare())

    assert provider.requests == ()
    assert manager._ledger.summary is None
    assert prepared.pressure.projected_input_tokens < 40_000
    provider.assert_exhausted()


def test_tier3_failure_is_atomic_and_not_retried_until_new_content() -> None:
    provider = ScriptedModelProvider(
        script=(
            (TextDelta(text="s"), _summary_completion("## Progress\nonly progress")),
        )
    )
    manager = _engine(_failure_policy(), provider)
    _long_turn(manager, user_text="one", length=90_000)
    _long_turn(manager, user_text="two", length=90_000)
    _long_turn(manager, user_text="three", length=90_000)

    first = _run(manager.prepare())
    second = _run(manager.prepare())

    assert first.operations == ()
    assert manager.history[1] == UserMessage.text("one")
    assert manager._ledger.summary is None
    assert len(provider.requests) == 1
    assert second.operations == ()
    assert len(provider.requests) == 1
    provider.assert_exhausted()


def test_tier3_fails_atomically_on_provider_exception() -> None:
    class ExplodingProvider:
        def __init__(self) -> None:
            self.requests: list[object] = []

        async def generate(self, request: object):
            self.requests.append(request)
            raise RuntimeError("provider unavailable")
            yield

    provider = ExplodingProvider()
    manager = _engine(_failure_policy(), provider)
    _long_turn(manager, user_text="one", length=90_000)
    _long_turn(manager, user_text="two", length=90_000)
    _long_turn(manager, user_text="three", length=90_000)

    prepared = _run(manager.prepare())

    assert prepared.operations == ()
    assert manager._ledger.summary is None
    assert len(provider.requests) == 1
    history = manager.history
    assert history[1] == UserMessage.text("one")


def test_tier3_fails_atomically_when_summary_exceeds_budget() -> None:
    oversized = SUMMARY_TEXT + ("\n" + "y" * 70_000)
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion(oversized)),)
    )
    manager = _engine(_failure_policy(), provider)
    _long_turn(manager, user_text="one", length=90_000)
    _long_turn(manager, user_text="two", length=90_000)
    _long_turn(manager, user_text="three", length=90_000)

    prepared = _run(manager.prepare())

    assert prepared.operations == ()
    assert manager._ledger.summary is None
    assert manager.history[1] == UserMessage.text("one")


def test_tier3_preserves_call_id_pairing_after_commit() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    manager = _engine(_policy(), provider)
    call = ToolCall(call_id="call_x", name="exec", arguments={"command": "x"})
    _apply(manager, UserMessage.text("one"))
    _apply(manager, AssistantMessage(content=(call,)))
    _apply(
        manager,
        ToolResultMessage(content=(ToolResult(call_id="call_x", output={"ok": True}),)),
    )
    _apply(manager, AssistantMessage.text("x" * 80_000))
    _long_turn(manager, user_text="two", length=80_000)

    _run(manager.prepare())

    history = manager.history
    assert history[1].content[0].text.startswith(SUMMARY_DELIMITER_OPEN)
    for message in history[2:]:
        if isinstance(message, ToolResultMessage):
            assert message.content[0].call_id == "call_x"
    provider.assert_exhausted()
