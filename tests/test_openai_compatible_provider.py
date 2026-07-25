import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    OpenAICompatibleModelProvider,
    TextDelta,
    UserMessage,
)


def test_streams_a_real_model_request_through_the_provider_neutral_seam() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"role":"assistant"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"content":"Hel"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"content":"lo"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1/",
        transport=httpx.MockTransport(respond),
    )
    model_request = ModelRequest(
        messages=(
            UserMessage.text("Hello"),
            AssistantMessage.text("How can I help?"),
            UserMessage.text("Reply briefly."),
        )
    )

    events = asyncio.run(_collect_events(provider, model_request))

    assert len(requests) == 1
    assert requests[0].url == "https://models.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer secret-key"
    assert json.loads(requests[0].content) == {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "How can I help?"},
            {"role": "user", "content": "Reply briefly."},
        ],
        "stream": True,
    }
    assert events == (
        TextDelta(text="Hel"),
        TextDelta(text="lo"),
        ModelCompletion(
            message=AssistantMessage.text("Hello"),
            finish_reason="stop",
        ),
    )


async def _collect_events(
    provider: ModelProvider,
    request: ModelRequest,
) -> tuple[ModelEvent, ...]:
    events: AsyncIterator[ModelEvent] = provider.generate(request)
    return tuple([event async for event in events])
