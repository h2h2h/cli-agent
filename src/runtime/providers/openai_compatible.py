"""OpenAI-compatible Chat Completions model provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from runtime.model import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    TextDelta,
    UserMessage,
)


class OpenAICompatibleModelProvider:
    """Stream text from an OpenAI-compatible Chat Completions endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._transport = transport

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        assistant_text: list[str] = []

        async with httpx.AsyncClient(transport=self._transport) as client:
            async with client.stream(
                "POST",
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "text/event-stream",
                },
                json={
                    "model": self._model,
                    "messages": [_message_payload(message) for message in request.messages],
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue

                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        return

                    chunk = json.loads(data)
                    choices = chunk.get("choices", ())
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        assistant_text.append(content)
                        yield TextDelta(text=content)

                    finish_reason = choice.get("finish_reason")
                    if finish_reason is not None:
                        yield ModelCompletion(
                            message=AssistantMessage.text("".join(assistant_text)),
                            finish_reason=finish_reason,
                        )
                        return


def _message_payload(message: UserMessage | AssistantMessage) -> dict[str, str]:
    role = "user" if isinstance(message, UserMessage) else "assistant"
    content = "".join(block.text for block in message.content)
    return {"role": role, "content": content}
