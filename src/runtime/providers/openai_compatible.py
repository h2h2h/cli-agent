"""OpenAI-compatible Chat Completions model provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from runtime._builtin_tools import ToolSchema
from runtime.model import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelUsage,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
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
        tool_call_fragments: dict[int, _ToolCallFragments] = {}
        tool_calls: tuple[ToolCall, ...] = ()
        finish_reason: str | None = None
        usage: ModelUsage | None = None

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
                    "stream_options": {"include_usage": True},
                },
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue

                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break

                    chunk = json.loads(data)
                    usage_payload = chunk.get("usage")
                    if usage_payload is not None:
                        usage = ModelUsage(
                            input_tokens=usage_payload["prompt_tokens"],
                            output_tokens=usage_payload["completion_tokens"],
                            total_tokens=usage_payload["total_tokens"],
                        )

                    choices = chunk.get("choices", ())
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        assistant_text.append(content)
                        yield TextDelta(text=content)

                    _accumulate_tool_call_fragments(
                        tool_call_fragments,
                        delta.get("tool_calls") or (),
                    )

                    current_finish_reason = choice.get("finish_reason")
                    if current_finish_reason is not None and finish_reason is None:
                        finish_reason = current_finish_reason
                        tool_calls = _assemble_tool_calls(tool_call_fragments)
                        for call in tool_calls:
                            yield ToolCallReady(call=call)

        if finish_reason is not None:
            yield ModelCompletion(
                message=_assistant_message(assistant_text, tool_calls),
                finish_reason=finish_reason,
                usage=usage,
            )


@dataclass(slots=True)
class _ToolCallFragments:
    call_id: str | None = None
    name: str | None = None
    arguments: list[str] = field(default_factory=list)


def _accumulate_tool_call_fragments(
    accumulated: dict[int, _ToolCallFragments],
    fragments: list[dict[str, object]],
) -> None:
    for fragment in fragments:
        index = fragment.get("index")
        if index is None:
            raise ValueError("streamed Tool Call is missing index")

        call = accumulated.setdefault(index, _ToolCallFragments())
        call_id = fragment.get("id")
        if call_id is not None:
            call.call_id = call_id

        function = fragment.get("function") or {}
        name = function.get("name")
        if name is not None:
            call.name = name
        arguments = function.get("arguments")
        if arguments is not None:
            call.arguments.append(arguments)


def _assemble_tool_calls(
    accumulated: dict[int, _ToolCallFragments],
) -> tuple[ToolCall, ...]:
    calls = []
    for index in sorted(accumulated):
        fragments = accumulated[index]
        if not fragments.call_id:
            raise ValueError(f"streamed Tool Call at index {index} is missing id")
        if not fragments.name:
            raise ValueError(
                f"streamed Tool Call at index {index} is missing function name"
            )

        raw_arguments = "".join(fragments.arguments)
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"streamed Tool Call at index {index} has invalid argument JSON"
            ) from exc
        if not isinstance(arguments, dict):
            raise ValueError(
                f"streamed Tool Call at index {index} arguments must be a JSON object"
            )

        calls.append(
            ToolCall(
                call_id=fragments.call_id,
                name=fragments.name,
                arguments=arguments,
            )
        )
    return tuple(calls)


def _assistant_message(
    assistant_text: list[str],
    tool_calls: tuple[ToolCall, ...],
) -> AssistantMessage:
    text = "".join(assistant_text)
    content: list[TextBlock | ToolCall] = []
    if text or not tool_calls:
        content.append(TextBlock(text=text))
    content.extend(tool_calls)
    return AssistantMessage(content=tuple(content))


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
