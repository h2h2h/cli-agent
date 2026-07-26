import asyncio
from collections.abc import AsyncIterator

import pytest

from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    ScriptedModelProvider,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UserMessage,
)


def test_records_multiple_requests_and_preserves_event_order() -> None:
    first_request = ModelRequest(messages=(UserMessage.text("Inspect files"),))
    second_request = ModelRequest(
        messages=(
            UserMessage.text("Inspect files"),
            AssistantMessage.text("Inspection complete."),
        )
    )
    call = ToolCall(call_id="call_1", name="exec", arguments={"command": "ls"})
    first_message = AssistantMessage(
        content=(TextBlock(text="Checking."), call),
    )
    first_events = (
        TextDelta(text="Checking."),
        ToolCallReady(call=call),
        ModelCompletion(message=first_message, finish_reason="tool_calls"),
    )
    second_events = (
        TextDelta(text="Done."),
        ModelCompletion(
            message=AssistantMessage.text("Done."),
            finish_reason="stop",
        ),
    )
    provider = ScriptedModelProvider(script=(first_events, second_events))

    observed_first = asyncio.run(_collect_events(provider, first_request))
    observed_second = asyncio.run(_collect_events(provider, second_request))

    assert observed_first == first_events
    assert observed_second == second_events
    assert provider.requests == (first_request, second_request)
    assert tuple(schema.name for schema in provider.requests[0].tools) == (
        "exec",
        "output",
        "kill",
    )
    provider.assert_exhausted()


def test_fails_when_more_requests_arrive_than_scripted() -> None:
    provider = ScriptedModelProvider(script=((),))
    request = ModelRequest(messages=())

    assert asyncio.run(_collect_events(provider, request)) == ()

    with pytest.raises(
        RuntimeError,
        match=r"more model requests than scripted: expected 1, received 2",
    ):
        asyncio.run(_collect_events(provider, request))

    assert provider.requests == (request, request)


def test_fails_when_scripted_requests_remain_unused() -> None:
    provider = ScriptedModelProvider(script=((), ()))
    request = ModelRequest(messages=())

    asyncio.run(_collect_events(provider, request))

    with pytest.raises(
        RuntimeError,
        match=r"fewer model requests than scripted: expected 2, received 1; "
        r"1 stream\(s\) remain",
    ):
        provider.assert_exhausted()

    asyncio.run(_collect_events(provider, request))
    provider.assert_exhausted()


async def _collect_events(
    provider: ScriptedModelProvider,
    request: ModelRequest,
) -> tuple[ModelEvent, ...]:
    events: AsyncIterator[ModelEvent] = provider.generate(request)
    return tuple([event async for event in events])
