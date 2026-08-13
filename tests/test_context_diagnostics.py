import asyncio

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
from cli_agent.runtime._context.manager import _ContextManager
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
    manager: _ContextManager,
    *,
    chunk_count: int = 200,
    chunk_chars: int = 400,
) -> None:
    call = _call()
    manager.append(UserMessage.text("one"))
    manager.append(AssistantMessage(content=(call,)))
    manager.append(
        ToolResultMessage(
            content=(
                ToolResult(
                    call_id=call.call_id, output=_snapshot(chunk_count, chunk_chars)
                ),
            )
        )
    )
    manager.append(AssistantMessage.text("done"))


def _append_recent_turn(manager: _ContextManager) -> None:
    call = _call("call_two")
    manager.append(UserMessage.text("two"))
    manager.append(AssistantMessage(content=(call,)))
    manager.append(
        ToolResultMessage(
            content=(ToolResult(call_id=call.call_id, output=_snapshot(200, 4000)),)
        )
    )
    manager.append(AssistantMessage.text("done"))


def _run(prepare_coroutine):
    return asyncio.run(prepare_coroutine)


def test_force_prepare_invalidates_anchor_and_exhausts_tiers() -> None:
    received: list[RuntimeDiagnostic] = []
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(budget=250_000, minimum_reclaim_tokens=100_000),
        provider=ScriptedModelProvider(script=()),
        session_id=SESSION_ID,
        on_diagnostic=received.append,
    )
    _append_old_turn(manager)
    _append_recent_turn(manager)
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    prepared = _run(manager.prepare_request())
    assert prepared.operations == ()
    manager.observe(
        prepared.revision,
        ModelUsage(input_tokens=1_000, output_tokens=10, total_tokens=1_010),
    )

    recovered = _run(manager.force_prepare())

    assert recovered is not None
    assert manager._anchor_input_tokens is None
    assert recovered.pressure.usage_source == "estimated"
    assert recovered.pressure.projected_input_tokens <= 250_000
    results = [
        message for message in manager.history if isinstance(message, ToolResultMessage)
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


def test_force_prepare_returns_none_when_unrecoverable() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=ScriptedModelProvider(script=()),
        session_id=SESSION_ID,
    )
    manager.append(UserMessage.text("x" * 200_000))

    assert _run(manager.force_prepare()) is None


def test_force_prepare_runs_tier3_with_a_complete_prefix() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion()),)
    )
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=provider,
        session_id=SESSION_ID,
    )
    manager.append(UserMessage.text("one"))
    manager.append(AssistantMessage.text("x" * 80_000))
    manager.append(UserMessage.text("two"))
    manager.append(AssistantMessage.text("x" * 80_000))

    recovered = _run(manager.force_prepare())

    assert recovered is not None
    assert recovered.pressure.projected_input_tokens <= 40_000
    assert manager._ledger.summary == SUMMARY_TEXT
    assert len(provider.requests) == 1
    provider.assert_exhausted()


def test_watermark_operations_emit_safe_diagnostics() -> None:
    received: list[RuntimeDiagnostic] = []
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(budget=250_000),
        provider=ScriptedModelProvider(script=()),
        session_id=SESSION_ID,
        on_diagnostic=received.append,
    )
    _append_old_turn(manager)
    _append_recent_turn(manager)
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    prepared = _run(manager.prepare_request())

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
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=ScriptedModelProvider(script=()),
        session_id=SESSION_ID,
        on_diagnostic=received.append,
    )
    manager.append(UserMessage.text("one"))
    call = _call()
    manager.append(AssistantMessage(content=(call,)))
    manager.append(
        ToolResultMessage(
            content=(ToolResult(call_id=call.call_id, output=_snapshot(400, 400)),)
        )
    )

    _run(manager.prepare_request())

    assert [diagnostic.kind for diagnostic in received] == ["context.oversized_result"]
    assert received[0].detail["reason"] == "oversized_result"


def test_tier3_failure_emits_compaction_failed_diagnostic() -> None:
    received: list[RuntimeDiagnostic] = []
    provider = ScriptedModelProvider(
        script=((TextDelta(text="s"), _summary_completion("## Progress\npartial")),)
    )
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(budget=60_000),
        provider=provider,
        session_id=SESSION_ID,
        on_diagnostic=received.append,
    )
    manager.append(UserMessage.text("one"))
    manager.append(AssistantMessage.text("x" * 115_000))
    manager.append(UserMessage.text("two"))
    manager.append(AssistantMessage.text("x" * 115_000))

    _run(manager.prepare_request())

    assert [diagnostic.kind for diagnostic in received] == ["context.compaction_failed"]
    assert received[0].detail["session_id"] == SESSION_ID
    assert "partial" not in str(received[0].detail)


def _summary_completion(text: str = SUMMARY_TEXT) -> ModelCompletion:
    return ModelCompletion(
        message=AssistantMessage.text(text),
        finish_reason="stop",
    )
