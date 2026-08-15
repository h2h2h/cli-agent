"""Session-cumulative token usage aggregation contract."""

import asyncio
from pathlib import Path

from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelMessage,
    ModelUsage,
    ScriptedModelProvider,
    SessionUsage,
    SystemMessage,
    ToolCall,
    ToolCallReady,
    ToolResult,
    UserMessage,
)
from cli_agent.runtime._agent_loop import AgentLoop
from cli_agent.runtime._context.engine import _ContextEngine

SYSTEM_MESSAGE = SystemMessage.text("System")
SESSION_ID = "test-session"
SUMMARY_TEXT = (
    "## Progress\nchecked the workspace\n"
    "## Files\nconfig.py edited\n"
    "## Todo\nrun the tests\n"
    "## Context\nuser prefers concise output"
)

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def _usage(*, input_tokens: int, output_tokens: int) -> ModelUsage:
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _completion(
    message: AssistantMessage,
    *,
    usage: ModelUsage | None = None,
) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop", usage=usage)


def _engine(
    provider: ScriptedModelProvider | None = None,
    policy: ContextPolicy | None = None,
) -> _ContextEngine:
    engine = _ContextEngine(
        session_id=SESSION_ID,
        context_policy=policy if policy is not None else _context_policy,
        provider=provider if provider is not None else ScriptedModelProvider(script=()),
    )
    engine.hydrate(system_message=SYSTEM_MESSAGE, snapshot=None, journal=(), revision=0)
    return engine


def _apply(engine: _ContextEngine, message: ModelMessage) -> None:
    engine.apply(message, engine.revision + 1)


def _long_turn(engine: _ContextEngine, *, user_text: str, length: int) -> None:
    _apply(engine, UserMessage.text(user_text))
    _apply(engine, AssistantMessage.text("x" * length))


def _tier3_policy() -> ContextPolicy:
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


def test_usage_starts_at_zero() -> None:
    engine = _engine()

    assert engine.usage == SessionUsage(input_tokens=0, output_tokens=0)


def test_observe_accumulates_across_revisions() -> None:
    engine = _engine()
    asyncio.run(engine.prepare())
    engine.observe_usage(_usage(input_tokens=10, output_tokens=20))
    _apply(engine, AssistantMessage.text("done"))
    asyncio.run(engine.prepare())
    engine.observe_usage(_usage(input_tokens=5, output_tokens=7))

    assert engine.usage == SessionUsage(input_tokens=15, output_tokens=27)


def test_observe_skips_missing_usage() -> None:
    engine = _engine()
    asyncio.run(engine.prepare())
    engine.observe_usage(None)
    _apply(engine, AssistantMessage.text("done"))
    asyncio.run(engine.prepare())
    engine.observe_usage(_usage(input_tokens=3, output_tokens=4))

    assert engine.usage == SessionUsage(input_tokens=3, output_tokens=4)


def test_tier3_summary_usage_accumulates_on_success() -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                _completion(
                    AssistantMessage.text(SUMMARY_TEXT),
                    usage=_usage(input_tokens=1_000, output_tokens=200),
                ),
            ),
        )
    )
    engine = _engine(provider, _tier3_policy())
    _long_turn(engine, user_text="one", length=80_000)
    _long_turn(engine, user_text="two", length=80_000)
    _long_turn(engine, user_text="three", length=80_000)

    prepared = asyncio.run(engine.prepare())

    assert prepared.operations and prepared.operations[0].tier == 3
    assert engine.usage == SessionUsage(input_tokens=1_000, output_tokens=200)
    provider.assert_exhausted()


def test_tier3_failed_summary_is_not_accumulated() -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                _completion(
                    AssistantMessage.text("## Progress\nonly progress"),
                    usage=_usage(input_tokens=1_000, output_tokens=200),
                ),
            ),
        )
    )
    engine = _engine(provider, _failure_policy())
    _long_turn(engine, user_text="one", length=90_000)
    _long_turn(engine, user_text="two", length=90_000)
    _long_turn(engine, user_text="three", length=90_000)

    prepared = asyncio.run(engine.prepare())

    assert prepared.operations == ()
    assert engine.usage == SessionUsage(input_tokens=0, output_tokens=0)
    provider.assert_exhausted()


class _KernelStub:
    async def reconcile_library(self) -> None:
        return None

    async def dispatch_batch(
        self,
        calls: tuple[ToolCall, ...],
    ) -> tuple[ToolResult, ...]:
        return tuple(ToolResult(call_id=call.call_id, output={}) for call in calls)


def _loop(provider: ScriptedModelProvider) -> AgentLoop:
    engine = _engine(provider)

    def commit(message: ModelMessage) -> int:
        del message
        return engine.revision + 1

    return AgentLoop(
        provider,
        _KernelStub(),  # type: ignore[arg-type]
        context=engine,
        commit=commit,
    )


def test_loop_usage_forwards_context_usage(tmp_path: Path) -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                _completion(
                    AssistantMessage.text("Hi"),
                    usage=_usage(input_tokens=4, output_tokens=6),
                ),
            ),
        )
    )
    loop = _loop(provider)

    assert loop.usage == SessionUsage(input_tokens=0, output_tokens=0)
    asyncio.run(_collect_events(loop, UserMessage.text("Hello")))

    assert loop.usage == SessionUsage(input_tokens=4, output_tokens=6)


def test_loop_accumulates_every_completion_within_one_turn(tmp_path: Path) -> None:
    call = ToolCall(call_id="call_1", name="exec", arguments={"command": "true"})
    tool_message = AssistantMessage(content=(call,))
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=call),
                _completion(
                    tool_message, usage=_usage(input_tokens=100, output_tokens=10)
                ),
            ),
            (
                _completion(
                    AssistantMessage.text("Done."),
                    usage=_usage(input_tokens=50, output_tokens=5),
                ),
            ),
        )
    )
    loop = _loop(provider)

    asyncio.run(_collect_events(loop, UserMessage.text("Inspect")))

    assert loop.usage == SessionUsage(input_tokens=150, output_tokens=15)
    provider.assert_exhausted()


async def _collect_events(
    loop: AgentLoop,
    user_message: UserMessage,
) -> tuple[object, ...]:
    return tuple([event async for event in loop.run(user_message)])


def test_session_usage_returns_none_for_unknown_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )

        assert runtime.session_usage() is None
        await runtime.close()

    asyncio.run(scenario())


def test_session_usage_accumulates_across_turns(tmp_path: Path) -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                _completion(
                    AssistantMessage.text("First"),
                    usage=_usage(input_tokens=10, output_tokens=20),
                ),
            ),
            (
                _completion(
                    AssistantMessage.text("Second"),
                    usage=_usage(input_tokens=3, output_tokens=5),
                ),
            ),
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
        )
        await _collect_turn(runtime, UserMessage.text("one"))

        assert runtime.session_usage() == SessionUsage(
            input_tokens=10,
            output_tokens=20,
        )

        await _collect_turn(runtime, UserMessage.text("two"))

        assert runtime.session_usage() == SessionUsage(
            input_tokens=13,
            output_tokens=25,
        )
        await runtime.close()

    asyncio.run(scenario())
    provider.assert_exhausted()


def test_session_usage_returns_none_after_session_close(tmp_path: Path) -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                _completion(
                    AssistantMessage.text("First"),
                    usage=_usage(input_tokens=10, output_tokens=20),
                ),
            ),
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
        )
        await _collect_turn(runtime, UserMessage.text("one"))

        assert runtime.session_usage() is not None
        await runtime.detach_session()

        assert runtime.session_usage() is None
        await runtime.close()

    asyncio.run(scenario())
    provider.assert_exhausted()


async def _collect_turn(
    runtime: AgentRuntime,
    message: UserMessage,
) -> tuple[object, ...]:
    if runtime._binding is None:
        await runtime.new_session()
    return tuple([event async for event in runtime.run_turn(message)])
