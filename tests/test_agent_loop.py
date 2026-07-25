import asyncio
from collections.abc import AsyncIterator

from runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    TextDelta,
    UserMessage,
)
from runtime._agent_loop import AgentLoop


def test_completes_a_text_only_turn() -> None:
    provider = GreetingProvider()
    loop = AgentLoop(provider)
    user_message = UserMessage.text("Hello")
    assistant_message = AssistantMessage.text("Hi")

    events = asyncio.run(_collect_events(loop, user_message))

    assert provider.requests == [
        ModelRequest(messages=(user_message,)),
    ]
    assert events == (
        TextDelta(text="H"),
        TextDelta(text="i"),
        ModelCompletion(
            message=assistant_message,
            finish_reason="stop",
        ),
    )
    assert loop.history == (user_message, assistant_message)


class GreetingProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        yield TextDelta(text="H")
        yield TextDelta(text="i")
        yield ModelCompletion(
            message=AssistantMessage.text("Hi"),
            finish_reason="stop",
        )


async def _collect_events(
    loop: AgentLoop,
    user_message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in loop.run(user_message)])
