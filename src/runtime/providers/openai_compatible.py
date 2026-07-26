"""OpenAI-compatible Chat Completions model provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from runtime._builtin_tools import ToolSchema
from runtime.model import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
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
                    "messages": [
                        payload
                        for message in request.messages
                        for payload in _message_payloads(message)
                    ],
                    "tools": [_tool_payload(tool) for tool in request.tools],
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


def _message_payloads(message: ModelMessage) -> tuple[dict[str, object], ...]:
    if isinstance(message, SystemMessage):
        return ({"role": "system", "content": _text_content(message.content)},)

    if isinstance(message, UserMessage):
        return ({"role": "user", "content": _text_content(message.content)},)

    if isinstance(message, AssistantMessage):
        payload: dict[str, object] = {
            "role": "assistant",
            "content": _text_content(
                tuple(
                    block for block in message.content if isinstance(block, TextBlock)
                )
            ),
        }
        calls = tuple(block for block in message.content if isinstance(block, ToolCall))
        if calls:
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in calls
            ]
        return (payload,)

    return tuple(
        {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": json.dumps(
                result.error if result.error is not None else result.output
            ),
        }
        for result in message.content
    )


def _text_content(content: tuple[TextBlock, ...]) -> str:
    return "".join(block.text for block in content)


def _tool_payload(tool: ToolSchema) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }
