import asyncio

import pytest

from cli_agent.errors.context import ContextExhaustedError
from cli_agent.runtime import (
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelUsage,
    ScriptedModelProvider,
    SystemMessage,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._context.engine import _ContextEngine
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

SYSTEM_MESSAGE = SystemMessage.text("System")
SESSION_ID = "session-diag"

SUMMARY_TEXT = (
    "## Progress\nchecked the workspace\n"
    "## Files\nconfig.py edited\n"
    "## Todo\nrun the tests\n"
    "## Context\nuser prefers concise output"
)


def _policy(*, budget: int = 40_000, **overrides: object) -> ContextPolicy:
    kwargs: dict[str, object] = {
        "context_window_tokens": budget + 5_000,
        "output_reserve_tokens": 5_000,
        "safety_margin_tokens": 0,
        "minimum_reclaim_tokens": 1,
    }
    kwargs.update(overrides)
    return ContextPolicy(**kwargs)  # type: ignore[arg-type]


def _engine(
    policy: ContextPolicy,
    provider: ScriptedModelProvider,
    received: list[RuntimeDiagnostic] | None = None,
) -> _ContextEngine:
    engine = _ContextEngine(
        session_id=SESSION_ID,
        context_policy=policy,
        provider=provider,
        on_diagnostic=received.append if received is not None else None,
    )
    engine.hydrate(system_message=SYSTEM_MESSAGE, snapshot=None, journal=(), revision=0)
    return engine


def _apply(engine: _ContextEngine, message: object) -> None:
    assert isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage))
    engine.apply(message, engine.revision + 1)


def _snapshot(chunk_count: int = 200, chunk_chars: int = 400) -> dict[str, object]:
    return {
        "ok": True,
        "exec_id": "exec_1",
        "status": "exited",
        "exit_code": 0,
        "chunks": [
            {
                "cursor": index,
                "stream": "stdout" if index % 2 == 0 else "stderr",
                "text": "x" * chunk_chars,
                "timestamp": "2026-01-01T00:00:00Z",
            }
            for index in range(chunk_count)
        ],
        "next_cursor": chunk_count,
        "is_terminal": True,
        "truncated": False,
        "available_from": 0,
    }


def _call(call_id: str = "call_1") -> ToolCall:
    return ToolCall(call_id=call_id, name="exec", arguments={"command": "inspect"})


def _append_old_turn(
    engine: _ContextEngine,
    *,
    chunk_count: int = 200,
    chunk_chars: int = 400,
) -> None:
    call = _call()
    _apply(engine, UserMessage.text("one"))
    _apply(engine, AssistantMessage(content=(call,)))
    _apply(
        engine,
        ToolResultMessage(
            content=(
                ToolResult(
                    call_id=call.call_id, output=_snapshot(chunk_count, chunk_chars)
                ),
            )
        ),
    )
    _apply(engine, AssistantMessage.text("done"))


def _append_recent_turn(engine: _ContextEngine) -> None:
    call = _call("call_two")
    _apply(engine, UserMessage.text("two"))
    _apply(engine, AssistantMessage(content=(call,)))
    _apply(
        engine,
        ToolResultMessage(
            content=(ToolResult(call_id=call.call_id, output=_snapshot(200, 4000)),)
        ),
    )
    _apply(engine, AssistantMessage.text("done"))


def _run(prepare_coroutine):
    return asyncio.run(prepare_coroutine)


def test_force_prepare_invalidates_anchor_and_exhausts_tiers() -> None:
    received: list[RuntimeDiagnostic] = []
    engine = _engine(
        _policy(budget=250_000, minimum_reclaim_tokens=100_000),
        ScriptedModelProvider(script=()),
        received,
    )
    _append_old_turn(engine)
    _append_recent_turn(engine)
    _apply(engine, UserMessage.text("three"))
    _apply(engine, AssistantMessage.text("final"))

    prepared = _run(engine.prepare())
    assert prepared.operations == ()
    engine.observe_usage(
        ModelUsage(input_tokens=1_000, output_tokens=10, total_tokens=1_010),
    )

    recovered = _run(engine.force_prepare())

    assert recovered.pressure.usage_source == "estimated"
    assert engine._anchor_input_tokens is None
    assert recovered.pressure.projected_input_tokens <= 250_000
    results = [
        message for message in engine.history if isinstance(message, ToolResultMessage)
    ]
    old = results[0].content[0]
    output = old.output
    assert isinstance(output, dict)
    assert output["reclaimed"]["state"] == "pruned"
    kinds = [diagnostic.kind for diagnostic in received]
    assert "context.snipped" in kinds
    assert "context.pruned" in kinds
    assert all(
        "one" not in str(diagnostic.detail) and "done" not in str(diagnostic.detail)
        for diagnostic in received
    )


def test_force_prepare_raises_host_error_when_unrecoverable() -> None:
    engine = _engine(_policy(), ScriptedModelProvider(script=()))
    _apply(engine, UserMessage.text("x" * 200_000))

    with pytest.raises(ContextExhaustedError) as raised:
        _run(engine.force_prepare())

    assert raised.value.code == "context_exhausted"
    assert raised.value.details["session_id"] == SESSION_ID


def test_force_prepare_runs_tier3_with_a_complete_prefix() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    engine = _engine(_policy(), provider)
    _apply(engine, UserMessage.text("one"))
    _apply(engine, AssistantMessage.text("x" * 80_000))
    _apply(engine, UserMessage.text("two"))
    _apply(engine, AssistantMessage.text("x" * 80_000))

    recovered = _run(engine.force_prepare())

    assert recovered.pressure.projected_input_tokens <= 40_000
    assert engine._ledger.summary == SUMMARY_TEXT
    assert len(provider.requests) == 1
    provider.assert_exhausted()


def test_watermark_operations_emit_safe_diagnostics() -> None:
    received: list[RuntimeDiagnostic] = []
    engine = _engine(
        _policy(budget=250_000),
        ScriptedModelProvider(script=()),
        received,
    )
    _append_old_turn(engine)
    _append_recent_turn(engine)
    _apply(engine, UserMessage.text("three"))
    _apply(engine, AssistantMessage.text("final"))

    prepared = _run(engine.prepare())

    assert prepared.operations
    kinds = [diagnostic.kind for diagnostic in received]
    assert kinds == ["context.snipped", "context.pruned"]
    for diagnostic in received:
        assert diagnostic.detail["session_id"] == SESSION_ID
        assert diagnostic.detail["tier"] in (1, 2)
        assert "revision_before" in diagnostic.detail
        assert "reason" in diagnostic.detail
        assert "one" not in str(diagnostic.detail)
        assert "exec_1" not in str(diagnostic.detail)
        assert "inspect" not in str(diagnostic.detail)


def test_oversized_guard_emits_oversized_result_diagnostic() -> None:
    received: list[RuntimeDiagnostic] = []
    engine = _engine(_policy(), ScriptedModelProvider(script=()), received)
    _apply(engine, UserMessage.text("one"))
    call = _call()
    _apply(engine, AssistantMessage(content=(call,)))
    _apply(
        engine,
        ToolResultMessage(
            content=(ToolResult(call_id=call.call_id, output=_snapshot(400, 400)),)
        ),
    )

    _run(engine.prepare())

    assert [diagnostic.kind for diagnostic in received] == ["context.oversized_result"]
    assert received[0].detail["reason"] == "oversized_result"


def test_tier3_failure_emits_compaction_failed_diagnostic() -> None:
    received: list[RuntimeDiagnostic] = []
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion("## Progress\npartial")),)
    )
    engine = _engine(_policy(budget=60_000), provider, received)
    _apply(engine, UserMessage.text("one"))
    _apply(engine, AssistantMessage.text("x" * 115_000))
    _apply(engine, UserMessage.text("two"))
    _apply(engine, AssistantMessage.text("x" * 115_000))

    _run(engine.prepare())

    assert [diagnostic.kind for diagnostic in received] == ["context.compaction_failed"]
    assert received[0].detail["session_id"] == SESSION_ID
    assert "partial" not in str(received[0].detail)


def _summary_completion(text: str = SUMMARY_TEXT) -> ModelCompletion:
    return ModelCompletion(
        message=AssistantMessage.text(text),
        finish_reason="stop",
    )
