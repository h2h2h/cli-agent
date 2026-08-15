"""Issue 08: the active-session Runtime state machine.

These tests pin the RFC-0018 acceptance criteria: one Runtime never binds
two Sessions at once, every lifecycle operation follows the formal
transition table (illegal transitions raise Host-facing errors), the
short ``_state_lock`` is never held across a long await, a failed
replacement rolls back without leaking a half-initialized binding, and
resume rebuilds the SystemMessage from the current Workspace / Capability
environment instead of replaying the stored config.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime.runtime as runtime_module
from cli_agent.errors import RuntimeStateError, SessionArchivedError
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    RuntimeClosedError,
    ScriptedModelProvider,
    SystemMessage,
    UserMessage,
)
from cli_agent.runtime.runtime import RuntimeState

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def _completion(message: AssistantMessage) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop")


def test_lifecycle_operations_follow_the_state_transition_table(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider(
        script=((_completion(AssistantMessage.text("A")),),)
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        assert runtime._state is RuntimeState.NO_SESSION

        session = await runtime.new_session()
        assert runtime._state is RuntimeState.ACTIVE_IDLE
        assert runtime._binding.session.session_id == session.session_id

        turn = runtime.run_turn(UserMessage.text("A"))
        assert isinstance(await turn.__anext__(), ModelCompletion)
        assert runtime._state is RuntimeState.RUNNING_TURN
        with pytest.raises(StopAsyncIteration):
            await turn.__anext__()
        assert runtime._state is RuntimeState.ACTIVE_IDLE

        await runtime.detach_session()
        assert runtime._state is RuntimeState.NO_SESSION
        assert runtime._binding is None

        await runtime.close()
        assert runtime._state is RuntimeState.CLOSED

    asyncio.run(scenario())


def test_run_turn_without_an_active_session_is_an_illegal_transition(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            with pytest.raises(RuntimeStateError) as raised:
                async for _ in runtime.run_turn(UserMessage.text("late")):
                    pass
            assert raised.value.code == "runtime_state"
            assert raised.value.details == {
                "action": "run_turn",
                "state": "no_session",
            }
            assert runtime._state is RuntimeState.NO_SESSION
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_lifecycle_operations_fail_closed_after_close(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        await runtime.close()
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await runtime.new_session()
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await runtime.detach_session()
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await runtime.resume_session("some-id")
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await runtime.archive_session("some-id")
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await runtime.unarchive_session("some-id")
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await runtime.delete_session("some-id")

    asyncio.run(scenario())


def test_concurrent_new_session_calls_serialize_on_one_binding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        sessions = await asyncio.gather(
            runtime.new_session(),
            runtime.new_session(),
            runtime.new_session(),
        )
        assert len({session.session_id for session in sessions}) == 3
        assert runtime._binding is not None
        assert runtime._binding.session.session_id == sessions[-1].session_id
        assert runtime._state is RuntimeState.ACTIVE_IDLE
        await runtime.close()

    asyncio.run(scenario())


def test_replacement_cancels_a_running_turn_from_another_task(
    tmp_path: Path,
) -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(
            self,
            request: ModelRequest,
        ) -> AsyncIterator[ModelEvent]:
            del request
            self.entered.set()
            await self.release.wait()
            yield _completion(AssistantMessage.text("late"))

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=BlockingProvider(),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        await runtime.new_session()
        old_kernel = runtime._binding.kernel

        turn = asyncio.create_task(
            _collect(runtime, UserMessage.text("wait"))
        )
        while runtime._state is not RuntimeState.RUNNING_TURN:
            await asyncio.sleep(0.01)

        await asyncio.wait_for(runtime.new_session(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert old_kernel._closed is True
        assert runtime._binding.kernel is not old_kernel
        assert runtime._state is RuntimeState.ACTIVE_IDLE
        await runtime.close()

    asyncio.run(scenario())


def test_failed_replacement_leaves_no_half_initialized_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        await runtime.new_session()
        first_kernel = runtime._binding.kernel

        class FailingAgentLoop:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("loop construction exploded")

        monkeypatch.setattr(runtime_module, "AgentLoop", FailingAgentLoop)

        with pytest.raises(RuntimeError, match="loop construction exploded"):
            await runtime.new_session()

        assert runtime._binding is None
        assert runtime._state is RuntimeState.NO_SESSION
        assert first_kernel._closed is True
        await runtime.close()

    asyncio.run(scenario())


def test_one_runtime_never_binds_two_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        first = await runtime.new_session()
        first_kernel = runtime._binding.kernel
        second = await runtime.new_session()

        assert first.session_id != second.session_id
        assert runtime._binding.session.session_id == second.session_id
        assert first_kernel._closed is True
        assert runtime._binding.kernel is not first_kernel
        await runtime.close()

    asyncio.run(scenario())


def test_archive_and_delete_of_the_active_session_detach_first(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        session = await runtime.new_session()
        kernel = runtime._binding.kernel

        await runtime.archive_session(session.session_id)
        assert kernel._closed is True
        assert runtime._binding is None
        assert runtime._state is RuntimeState.NO_SESSION

        with pytest.raises(SessionArchivedError):
            await runtime.resume_session(session.session_id)

        await runtime.unarchive_session(session.session_id)
        restored = await runtime.resume_session(session.session_id)
        assert restored.session_id == session.session_id

        await runtime.delete_session(session.session_id)
        assert runtime._binding is None
        with pytest.raises(Exception):
            await runtime.resume_session(session.session_id)
        await runtime.close()

    asyncio.run(scenario())


def test_resume_rebuilds_system_message_from_the_current_environment(
    tmp_path: Path,
) -> None:
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("legacy rule one\n", encoding="utf-8")
    first_provider = ScriptedModelProvider(
        script=((_completion(AssistantMessage.text("done")),),)
    )

    async def scenario() -> None:
        async with await AgentRuntime.open(
            workspace=tmp_path,
            provider=first_provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        ) as runtime:
            session = await runtime.new_session()
            async for _ in runtime.run_turn(UserMessage.text("work")):
                pass

        agents_md.write_text("current rule two\n", encoding="utf-8")
        resumed_provider = ScriptedModelProvider(
            script=((_completion(AssistantMessage.text("done")),),)
        )
        async with await AgentRuntime.open(
            workspace=tmp_path,
            provider=resumed_provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        ) as reopened:
            await reopened.resume_session(session.session_id)
            async for _ in reopened.run_turn(UserMessage.text("again")):
                pass

        stored_config = session.config.system_prompt
        assert "legacy rule one" in stored_config
        system = resumed_provider.requests[0].messages[0]
        assert isinstance(system, SystemMessage)
        text = "".join(block.text for block in system.content)
        assert "current rule two" in text
        assert "legacy rule one" not in text
        first_provider.assert_exhausted()
        resumed_provider.assert_exhausted()

    asyncio.run(scenario())


def test_state_lock_is_a_short_hold_synchronous_lock(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        assert isinstance(runtime._state_lock, threading.Lock)
        assert isinstance(runtime._lifecycle_op_lock, asyncio.Lock)
        await runtime.close()

    asyncio.run(scenario())


def test_state_lock_is_never_held_across_an_await() -> None:
    source = Path(runtime_module.__file__).read_text(encoding="utf-8").splitlines()
    inside = 0
    block_indent = 0
    for line in source:
        stripped = line.strip()
        if "with self._state_lock:" in stripped:
            inside += 1
            block_indent = len(line) - len(line.lstrip())
            continue
        if inside == 0:
            continue
        indent = len(line) - len(line.lstrip())
        if not stripped or indent <= block_indent:
            inside = 0
        elif "await" in stripped:
            raise AssertionError(
                f"await inside a state-lock section: {stripped}"
            )
    assert inside == 0


async def _collect(
    runtime: AgentRuntime,
    message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in runtime.run_turn(message)])
