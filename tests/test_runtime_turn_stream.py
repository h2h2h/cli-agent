"""Issue 009: Runtime-owned turn production and stream cancellation."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    ModelUsage,
    TextDelta,
    UserMessage,
)
from cli_agent.runtime._turn import TurnStream

_CONTEXT_POLICY = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


class _BlockingProvider:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request
        self.entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield  # pragma: no cover


def _completion(text: str) -> ModelCompletion:
    return ModelCompletion(
        message=AssistantMessage.text(text),
        finish_reason="stop",
    )


def test_run_turn_owns_a_separate_producer_task(tmp_path: Path) -> None:
    provider = _BlockingProvider()

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        await runtime.new_session()
        stream = runtime.run_turn(UserMessage.text("wait"))
        assert isinstance(stream, TurnStream)
        producer = runtime._turn_task
        assert producer is not None
        assert producer is not asyncio.current_task()

        consumer = asyncio.create_task(stream.__anext__())
        await provider.entered.wait()
        assert runtime._turn_task is producer
        assert consumer is not producer

        await runtime.close()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert provider.cancelled.is_set()
        assert producer.done()

    asyncio.run(scenario())


def test_queue_backpressure_stops_the_producer_without_a_consumer(
    tmp_path: Path,
) -> None:
    class _BurstProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.completed = asyncio.Event()

        async def generate(
            self,
            request: ModelRequest,
        ) -> AsyncIterator[ModelEvent]:
            del request
            self.started.set()
            for index in range(100):
                yield TextDelta(text=str(index))
            self.completed.set()
            yield _completion("done")

    async def scenario() -> None:
        provider = _BurstProvider()
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        await runtime.new_session()
        stream = runtime.run_turn(UserMessage.text("burst"))
        await provider.started.wait()
        await asyncio.sleep(0)

        assert stream._queue.maxsize == 64
        assert stream._queue.qsize() == 64
        assert not provider.completed.is_set()

        await stream.aclose()
        assert runtime._turn_task is None
        await runtime.close()

    asyncio.run(scenario())


def test_breaking_from_async_for_closes_the_producer(tmp_path: Path) -> None:
    class _StreamingProvider(_BlockingProvider):
        async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            del request
            self.entered.set()
            yield TextDelta(text="first")
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def scenario() -> None:
        provider = _StreamingProvider()
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        await runtime.new_session()
        async for _ in runtime.run_turn(UserMessage.text("break")):
            break
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert provider.cancelled.is_set()
        assert runtime._turn_task is None
        await runtime.close()

    asyncio.run(scenario())


def test_completion_usage_is_committed_with_the_assistant_message(
    tmp_path: Path,
) -> None:
    provider = _UsageProvider()

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_ScriptedInteraction("allow_once"),
            context_policy=_CONTEXT_POLICY,
        )
        session = await runtime.new_session()
        events = [event async for event in runtime.run_turn(UserMessage.text("usage"))]

        assert events == [
            ModelCompletion(
                message=AssistantMessage.text("done"),
                finish_reason="stop",
                usage=ModelUsage(input_tokens=7, output_tokens=3, total_tokens=10),
            )
        ]
        records = runtime._resources.session_store.load_usage_records(
            session.session_id
        )
        assert len(records) == 1
        assert records[0].purpose == "agent"
        assert records[0].input_tokens == 7
        assert records[0].output_tokens == 3
        _, journal = runtime._resources.session_store.load(session.session_id)
        assert journal[-1] == AssistantMessage.text("done")
        await runtime.close()

    asyncio.run(scenario())


class _UsageProvider:
    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request
        yield ModelCompletion(
            message=AssistantMessage.text("done"),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=7, output_tokens=3, total_tokens=10),
        )
