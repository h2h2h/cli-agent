import asyncio

import pytest

from cli_agent.runtime import (
    AssistantMessage,
    ContextPolicy,
    ScriptedModelProvider,
    SystemMessage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._context.manager import (
    ContextOverflowError,
    _ContextManager,
)

SYSTEM_MESSAGE = SystemMessage.text("System")
PROVIDER = ScriptedModelProvider(script=())
SESSION_ID = "test-session"


def _snapshot(
    *,
    chunk_count: int = 200,
    chunk_chars: int = 400,
    exec_id: str = "exec_1",
) -> dict[str, object]:
    return {
        "ok": True,
        "exec_id": exec_id,
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


def _policy(*, budget: int = 250_000, **overrides: object) -> ContextPolicy:
    kwargs: dict[str, object] = {
        "context_window_tokens": budget + 5_000,
        "output_reserve_tokens": 5_000,
        "safety_margin_tokens": 0,
        "minimum_reclaim_tokens": 1,
    }
    kwargs.update(overrides)
    return ContextPolicy(**kwargs)  # type: ignore[arg-type]


def _call(call_id: str, name: str = "exec") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments={"command": "inspect"})


def _result(
    call: ToolCall, *, output: object | None, error: object | None = None
) -> ToolResult:
    return ToolResult(call_id=call.call_id, output=output, error=error)


def _append_complete_turn(
    manager: _ContextManager,
    *,
    user_text: str,
    call: ToolCall,
    output: object | None,
) -> None:
    manager.append(UserMessage.text(user_text))
    manager.append(AssistantMessage(content=(call,)))
    manager.append(ToolResultMessage(content=(_result(call, output=output),)))
    manager.append(AssistantMessage.text("done"))


def _append_active_turn(
    manager: _ContextManager,
    *,
    user_text: str,
    call: ToolCall,
    output: object | None,
) -> None:
    manager.append(UserMessage.text(user_text))
    manager.append(AssistantMessage(content=(call,)))
    manager.append(ToolResultMessage(content=(_result(call, output=output),)))


def _result_messages(manager: _ContextManager) -> list[ToolResultMessage]:
    return [m for m in manager.history if isinstance(m, ToolResultMessage)]


def _state(result: ToolResult) -> str | None:
    output = result.output
    if isinstance(output, dict):
        reclaimed = output.get("reclaimed")
        if isinstance(reclaimed, dict):
            return reclaimed["state"]
    return None


async def _prepare(manager: _ContextManager):
    return await manager.prepare_request()


def _run(prepare_coroutine):
    return asyncio.run(prepare_coroutine)


def test_tier1_snips_oldest_candidates_until_snip_target() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    _append_complete_turn(
        manager,
        user_text="zero",
        call=_call("call_zero"),
        output=_snapshot(exec_id="exec_zero", chunk_count=400, chunk_chars=400),
    )
    _append_complete_turn(
        manager,
        user_text="one",
        call=_call("call_one"),
        output=_snapshot(exec_id="exec_one", chunk_count=200, chunk_chars=4000),
    )
    _append_complete_turn(
        manager,
        user_text="two",
        call=_call("call_two"),
        output=_snapshot(exec_id="exec_two", chunk_count=80, chunk_chars=4000),
    )
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    prepared = _run(_prepare(manager))

    zero, one, two = _result_messages(manager)
    assert _state(zero.content[0]) == "snipped"
    assert _state(one.content[0]) == "snipped"
    assert _state(two.content[0]) is None
    assert len(prepared.operations) == 1
    operation = prepared.operations[0]
    assert operation.tier == 1
    assert operation.reason == "watermark"
    assert operation.entries_changed == 2
    assert operation.input_tokens_before > operation.input_tokens_after
    assert operation.revision_after > operation.revision_before
    assert prepared.pressure.projected_input_tokens < _policy().input_budget


def test_tier2_prunes_snipped_candidates_when_tier1_cannot_reach_target() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    _append_complete_turn(
        manager,
        user_text="one",
        call=_call("call_one"),
        output=_snapshot(exec_id="exec_one", chunk_count=200, chunk_chars=400),
    )
    _append_complete_turn(
        manager,
        user_text="two",
        call=_call("call_two"),
        output=_snapshot(exec_id="exec_two", chunk_count=200, chunk_chars=4000),
    )
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    prepared = _run(_prepare(manager))

    one, two = _result_messages(manager)
    assert _state(one.content[0]) == "pruned"
    assert _state(two.content[0]) is None
    tiers = [operation.tier for operation in prepared.operations]
    assert tiers == [1, 2]
    assert all(operation.reason == "watermark" for operation in prepared.operations)


def test_repeated_prepare_is_idempotent_and_monotonic() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    _append_complete_turn(
        manager,
        user_text="one",
        call=_call("call_one"),
        output=_snapshot(exec_id="exec_one", chunk_count=200, chunk_chars=4000),
    )
    _append_complete_turn(
        manager,
        user_text="two",
        call=_call("call_two"),
        output=_snapshot(exec_id="exec_two", chunk_count=80, chunk_chars=4000),
    )
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    first = _run(_prepare(manager))
    second = _run(_prepare(manager))

    assert first.operations
    assert second.operations == ()
    assert first.revision == second.revision
    one, two = _result_messages(manager)
    assert _state(one.content[0]) == "snipped"
    assert _state(two.content[0]) is None


def test_protected_suffix_keeps_active_and_recent_turns_untouched() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(budget=100_000),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    _append_complete_turn(
        manager,
        user_text="one",
        call=_call("call_one"),
        output=_snapshot(exec_id="exec_one", chunk_count=400, chunk_chars=400),
    )
    _append_complete_turn(
        manager,
        user_text="two",
        call=_call("call_two"),
        output=_snapshot(exec_id="exec_two", chunk_count=400, chunk_chars=400),
    )
    _append_active_turn(
        manager,
        user_text="three",
        call=_call("call_three"),
        output=_snapshot(exec_id="exec_three", chunk_count=400, chunk_chars=400),
    )

    prepared = _run(_prepare(manager))

    one, two, three = _result_messages(manager)
    assert _state(one.content[0]) == "pruned"
    assert _state(two.content[0]) is None
    assert _state(three.content[0]) is None
    assert prepared.pressure.projected_input_tokens < 100_000


def test_excluded_tools_are_never_reduced() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(excluded_tools=frozenset({"exec"})),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    exec_call = _call("call_exec", name="exec")
    output_call = _call("call_output", name="output")
    manager.append(UserMessage.text("one"))
    manager.append(AssistantMessage(content=(exec_call, output_call)))
    manager.append(
        ToolResultMessage(
            content=(
                _result(exec_call, output=_snapshot(exec_id="exec_one")),
                _result(output_call, output=_snapshot(exec_id="output_one")),
            )
        )
    )
    manager.append(AssistantMessage.text("done"))
    _append_complete_turn(
        manager,
        user_text="two",
        call=_call("call_two"),
        output=_snapshot(exec_id="exec_two", chunk_count=200, chunk_chars=4000),
    )
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    _run(_prepare(manager))

    excluded, allowed = _result_messages(manager)[0].content
    assert _state(excluded) is None
    assert _state(allowed) == "pruned"


def test_error_and_unknown_payloads_are_skipped() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    error_call = _call("call_error")
    unknown_call = _call("call_unknown")
    manager.append(UserMessage.text("one"))
    manager.append(AssistantMessage(content=(error_call, unknown_call)))
    manager.append(
        ToolResultMessage(
            content=(
                _result(
                    error_call,
                    output=None,
                    error={"ok": False, "code": "policy_denied", "message": "no"},
                ),
                _result(unknown_call, output={"custom": "payload"}),
            )
        )
    )
    manager.append(AssistantMessage.text("done"))
    _append_complete_turn(
        manager,
        user_text="two",
        call=_call("call_two"),
        output=_snapshot(exec_id="exec_two", chunk_count=200, chunk_chars=4000),
    )
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    prepared = _run(_prepare(manager))

    error_result, unknown_result = _result_messages(manager)[0].content
    assert error_result.error is not None
    assert unknown_result.output == {"custom": "payload"}
    assert prepared.operations == ()


def test_minimum_reclaim_blocks_small_results() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(minimum_reclaim_tokens=100_000),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    _append_complete_turn(
        manager,
        user_text="one",
        call=_call("call_one"),
        output=_snapshot(exec_id="exec_one", chunk_count=200, chunk_chars=400),
    )
    _append_complete_turn(
        manager,
        user_text="two",
        call=_call("call_two"),
        output=_snapshot(exec_id="exec_two", chunk_count=200, chunk_chars=4000),
    )
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    prepared = _run(_prepare(manager))

    assert prepared.operations == ()
    one, _two = _result_messages(manager)
    assert _state(one.content[0]) is None


def test_oversized_guard_snips_the_active_turn_result() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(budget=40_000),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    _append_active_turn(
        manager,
        user_text="one",
        call=_call("call_one"),
        output=_snapshot(exec_id="exec_one", chunk_count=400, chunk_chars=400),
    )

    prepared = _run(_prepare(manager))

    (result_message,) = _result_messages(manager)
    assert _state(result_message.content[0]) == "snipped"
    assert prepared.pressure.projected_input_tokens <= 40_000
    assert len(prepared.operations) == 1
    operation = prepared.operations[0]
    assert operation.reason == "oversized_result"
    assert operation.tier == 1
    assert operation.entries_changed == 1


def test_oversized_user_input_fails_closed() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(budget=40_000),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    manager.append(UserMessage.text("x" * 200_000))

    with pytest.raises(ContextOverflowError, match="cannot be reduced safely"):
        _run(_prepare(manager))


def test_compaction_preserves_call_id_pairing_and_message_order() -> None:
    manager = _ContextManager(
        system_message=SYSTEM_MESSAGE,
        context_policy=_policy(),
        provider=PROVIDER,
        session_id=SESSION_ID,
    )
    first_call = _call("call_one")
    second_call = _call("call_two")
    _append_complete_turn(
        manager,
        user_text="one",
        call=first_call,
        output=_snapshot(exec_id="exec_one", chunk_count=400, chunk_chars=400),
    )
    _append_complete_turn(
        manager,
        user_text="two",
        call=second_call,
        output=_snapshot(exec_id="exec_two", chunk_count=80, chunk_chars=4000),
    )
    manager.append(UserMessage.text("three"))
    manager.append(AssistantMessage.text("final"))

    before = manager.history
    _run(_prepare(manager))
    after = manager.history

    assert [type(m) for m in before] == [type(m) for m in after]
    for message_index, message in enumerate(after):
        if not isinstance(message, ToolResultMessage):
            continue
        previous = after[message_index - 1]
        assert isinstance(previous, AssistantMessage)
        expected = {
            block.call_id for block in previous.content if isinstance(block, ToolCall)
        }
        assert {result.call_id for result in message.content} == expected
