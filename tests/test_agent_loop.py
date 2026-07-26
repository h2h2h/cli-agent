import asyncio

from runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    ScriptedModelProvider,
    TextDelta,
    UserMessage,
)
from runtime._agent_loop import AgentLoop


def test_completes_a_text_only_turn() -> None:
    user_message = UserMessage.text("Hello")
    assistant_message = AssistantMessage.text("Hi")
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="H"),
                TextDelta(text="i"),
                ModelCompletion(
                    message=assistant_message,
                    finish_reason="stop",
                ),
            ),
        )
    )
    loop = AgentLoop(provider)

    events = asyncio.run(_collect_events(loop, user_message))

    assert provider.requests == (ModelRequest(messages=(user_message,)),)
    assert events == (
        TextDelta(text="H"),
        TextDelta(text="i"),
        ModelCompletion(
            message=assistant_message,
            finish_reason="stop",
        ),
    )
    assert loop.history == (user_message, assistant_message)
    provider.assert_exhausted()


async def _collect_events(
    loop: AgentLoop,
    user_message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in loop.run(user_message)])
