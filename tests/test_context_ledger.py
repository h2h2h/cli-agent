import asyncio

import pytest

from cli_agent.runtime import (
    AssistantMessage,
    ContextPolicy,
    ModelRequest,
    ModelUsage,
    SystemMessage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._context_manager import (
    _ContextLedger,
    _ContextLedgerError,
    _ContextManager,
    estimate_message_tokens,
    estimate_request_tokens,
)

SYSTEM_MESSAGE = SystemMessage.text("System instruction")
CONTEXT_POLICY = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)

_exec = ToolCall(call_id="call_exec", name="exec", arguments={"command": "ls"})
_output = ToolCall(call_id="call_output", name="output", arguments={"exec_id": "e1"})


def test_ledger_starts_with_system_message_and_tracks_revision() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)

    assert ledger.history == (SYSTEM_MESSAGE,)
    assert ledger.revision == 0
    assert ledger.message_count == 1

    ledger.append(UserMessage.text("Hello"))
    ledger.append(AssistantMessage.text("Hi"))

    assert ledger.history == (
        SYSTEM_MESSAGE,
        UserMessage.text("Hello"),
        AssistantMessage.text("Hi"),
    )
    assert ledger.revision == 2
    assert ledger.message_count == 3


def test_ledger_rejects_system_message_append() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)

    with pytest.raises(_ContextLedgerError, match="system message cannot be appended"):
        ledger.append(SystemMessage.text("Another system message"))


def test_ledger_accepts_parallel_exchange_in_any_result_order() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Run both"))
    ledger.append(AssistantMessage(content=(_exec, _output)))
    ledger.append(
        ToolResultMessage(
            content=(
                ToolResult(call_id=_output.call_id, output={"ok": True}),
                ToolResult(call_id=_exec.call_id, output={"ok": True}),
            )
        )
    )
    ledger.append(AssistantMessage.text("Done"))

    assert ledger.revision == 4


def test_ledger_rejects_missing_tool_result_call_id() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Run both"))
    ledger.append(AssistantMessage(content=(_exec, _output)))

    with pytest.raises(_ContextLedgerError, match="missing call_id 'call_output'"):
        ledger.append(
            ToolResultMessage(
                content=(ToolResult(call_id=_exec.call_id, output={"ok": True}),)
            )
        )


def test_ledger_rejects_extra_or_cross_turn_tool_result_call_id() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Run both"))
    ledger.append(AssistantMessage(content=(_exec, _output)))

    with pytest.raises(_ContextLedgerError, match="no matching tool call"):
        ledger.append(
            ToolResultMessage(
                content=(
                    ToolResult(call_id=_exec.call_id, output={"ok": True}),
                    ToolResult(call_id="call_stale", output={"ok": True}),
                )
            )
        )


def test_ledger_rejects_duplicate_tool_result_call_id() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Run one"))
    ledger.append(AssistantMessage(content=(_exec,)))

    with pytest.raises(_ContextLedgerError, match="duplicate tool result"):
        ledger.append(
            ToolResultMessage(
                content=(
                    ToolResult(call_id=_exec.call_id, output={"ok": True}),
                    ToolResult(call_id=_exec.call_id, output={"ok": True}),
                )
            )
        )


def test_ledger_rejects_tool_result_without_preceding_tool_call() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Hi"))

    with pytest.raises(_ContextLedgerError, match="without a preceding tool call"):
        ledger.append(
            ToolResultMessage(
                content=(ToolResult(call_id=_exec.call_id, output={"ok": True}),)
            )
        )


def test_ledger_rejects_assistant_after_assistant_message() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Hi"))
    ledger.append(AssistantMessage.text("First"))

    with pytest.raises(
        _ContextLedgerError,
        match="must follow a user or tool result message",
    ):
        ledger.append(AssistantMessage.text("Second"))


def test_ledger_rejects_duplicate_assistant_tool_call_id() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Hi"))

    with pytest.raises(_ContextLedgerError, match="duplicate tool call_id"):
        ledger.append(AssistantMessage(content=(_exec, _exec)))


def test_ledger_allows_new_user_turn_after_abandoned_active_turn() -> None:
    ledger = _ContextLedger(SYSTEM_MESSAGE)
    ledger.append(UserMessage.text("Run"))
    ledger.append(AssistantMessage(content=(_exec,)))
    ledger.append(UserMessage.text("Continue"))

    assert ledger.history[-1] == UserMessage.text("Continue")
    assert ledger.revision == 3


def test_manager_prepares_immutable_requests_with_pressure() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
    )
    user_message = UserMessage.text("Hello")
    manager.append(user_message)

    prepared = asyncio.run(manager.prepare_request())

    assert prepared.revision == 1
    assert prepared.request == ModelRequest(messages=(SYSTEM_MESSAGE, user_message))
    assert prepared.pressure.input_budget == 14_336
    assert prepared.pressure.projected_input_tokens == estimate_request_tokens(
        prepared.request
    )
    assert prepared.pressure.usage_source == "estimated"
    assert prepared.pressure.ratio == prepared.pressure.projected_input_tokens / 14_336


def test_manager_prepares_before_every_model_step() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
    )
    manager.append(UserMessage.text("Run"))
    first = asyncio.run(manager.prepare_request())
    manager.append(AssistantMessage(content=(_exec,)))
    second = asyncio.run(manager.prepare_request())
    manager.append(
        ToolResultMessage(content=(ToolResult(call_id=_exec.call_id, output={}),))
    )
    third = asyncio.run(manager.prepare_request())

    assert (first.revision, second.revision, third.revision) == (1, 2, 3)
    assert second.request.messages == (
        SYSTEM_MESSAGE,
        UserMessage.text("Run"),
        AssistantMessage(content=(_exec,)),
    )
    assert third.request.messages[-1] == ToolResultMessage(
        content=(ToolResult(call_id=_exec.call_id, output={}),)
    )


def test_manager_uses_reported_anchor_and_estimates_the_delta() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
    )
    manager.append(UserMessage.text("Hello"))
    prepared = asyncio.run(manager.prepare_request())
    manager.observe(
        prepared.revision,
        ModelUsage(input_tokens=100, output_tokens=20, total_tokens=120),
    )
    assistant_message = AssistantMessage.text("World")
    manager.append(assistant_message)
    next_prepared = asyncio.run(manager.prepare_request())

    assert (
        next_prepared.pressure.projected_input_tokens
        == 100 + estimate_message_tokens(assistant_message)
    )
    assert next_prepared.pressure.usage_source == "estimated"
    assert next_prepared.revision == 2


def test_manager_reports_exact_anchor_when_nothing_was_appended() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
    )
    manager.append(UserMessage.text("Hello"))
    prepared = asyncio.run(manager.prepare_request())
    manager.observe(
        prepared.revision,
        ModelUsage(input_tokens=100, output_tokens=20, total_tokens=120),
    )

    next_prepared = asyncio.run(manager.prepare_request())

    assert next_prepared.pressure.projected_input_tokens == 100
    assert next_prepared.pressure.usage_source == "reported"


def test_manager_ignores_missing_usage_without_anchoring() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
    )
    manager.append(UserMessage.text("Hello"))
    prepared = asyncio.run(manager.prepare_request())
    manager.observe(prepared.revision, None)

    next_prepared = asyncio.run(manager.prepare_request())

    assert next_prepared.pressure.usage_source == "estimated"
    assert next_prepared.pressure.projected_input_tokens == estimate_request_tokens(
        next_prepared.request
    )


def test_manager_rejects_stale_and_duplicate_observations() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=CONTEXT_POLICY,
    )
    manager.append(UserMessage.text("Hello"))
    prepared = asyncio.run(manager.prepare_request())

    with pytest.raises(_ContextLedgerError, match="un-prepared revision"):
        manager.observe(prepared.revision + 1, None)
    with pytest.raises(_ContextLedgerError, match="un-prepared revision"):
        manager.observe(prepared.revision - 1, None)

    manager.observe(prepared.revision, None)
    with pytest.raises(_ContextLedgerError, match="called twice"):
        manager.observe(prepared.revision, None)


def test_text_estimator_pins_cjk_and_ascii_formula() -> None:
    assert estimate_message_tokens(UserMessage.text("hello")) == 6
    assert estimate_message_tokens(UserMessage.text("你好世界")) == 8
    assert estimate_message_tokens(UserMessage.text("a" * 5)) == 6
    assert estimate_message_tokens(AssistantMessage.text("a" * 4)) == 5


def test_request_estimator_includes_tool_schema_overhead() -> None:
    request = ModelRequest(messages=(UserMessage.text("Hello"),))
    bare = ModelRequest(messages=request.messages, tools=())

    assert estimate_request_tokens(request) > estimate_request_tokens(bare)
    assert estimate_request_tokens(bare) == estimate_message_tokens(
        UserMessage.text("Hello")
    )
