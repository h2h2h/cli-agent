import asyncio
from collections.abc import AsyncIterator

from runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    TextBlock,
    TextDelta,
    UserMessage,
)


def test_constructs_a_provider_neutral_text_conversation() -> None:
    user_message = UserMessage.text("Hello")
    assistant_message = AssistantMessage.text("Hi")

    request = ModelRequest(messages=(user_message, assistant_message))

    assert user_message == UserMessage(content=(TextBlock(text="Hello"),))
    assert assistant_message == AssistantMessage(content=(TextBlock(text="Hi"),))
    assert request.messages == (user_message, assistant_message)


def test_represents_streamed_text_and_terminal_completion() -> None:
    assistant_message = AssistantMessage.text("Hi")

    events = (
        TextDelta(text="H"),
        TextDelta(text="i"),
        ModelCompletion(message=assistant_message, finish_reason="stop"),
    )

    assert events[-1].message == assistant_message
    assert events[-1].finish_reason == "stop"


def test_provider_exposes_an_asynchronous_model_event_stream() -> None:
    request = ModelRequest(messages=(UserMessage.text("Hello"),))

    events = asyncio.run(_collect_events(GreetingProvider(), request))

    assert events == (
        TextDelta(text="Hi"),
        ModelCompletion(
            message=AssistantMessage.text("Hi"),
            finish_reason="stop",
        ),
    )


class GreetingProvider:
    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        assert request.messages == (UserMessage.text("Hello"),)
        yield TextDelta(text="Hi")
        yield ModelCompletion(
            message=AssistantMessage.text("Hi"),
            finish_reason="stop",
        )


async def _collect_events(
    provider: ModelProvider,
    request: ModelRequest,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in provider.generate(request)])
