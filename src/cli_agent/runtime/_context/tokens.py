"""Deterministic token estimates for model messages and requests."""

from __future__ import annotations

import json
import math

from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    ModelRequest,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def estimate_request_tokens(request: ModelRequest) -> int:
    """Return a conservative deterministic input token estimate for one request."""

    message_tokens = sum(
        estimate_message_tokens(message) for message in request.messages
    )
    tool_tokens = sum(
        8 + _estimate_text_tokens(_dump(tool.to_json())) for tool in request.tools
    )
    return message_tokens + tool_tokens


def estimate_message_tokens(message: ModelMessage) -> int:
    """Return a conservative deterministic input token estimate for one message."""

    if isinstance(message, SystemMessage | UserMessage):
        return 4 + _estimate_text_tokens(_join_text(message.content))
    if isinstance(message, AssistantMessage):
        text_blocks = tuple(
            block for block in message.content if isinstance(block, TextBlock)
        )
        calls = tuple(block for block in message.content if isinstance(block, ToolCall))
        call_tokens = sum(
            8 + _estimate_text_tokens(_dump(call.arguments)) for call in calls
        )
        return 4 + _estimate_text_tokens(_join_text(text_blocks)) + call_tokens
    assert isinstance(message, ToolResultMessage)
    return 4 + sum(
        8
        + _estimate_text_tokens(
            _dump(result.error if result.error is not None else result.output)
        )
        for result in message.content
    )


def _estimate_text_tokens(text: str) -> int:
    cjk = sum(
        1
        for char in text
        if "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"
    )
    return cjk + math.ceil((len(text) - cjk) / 4)


def _join_text(blocks: tuple[TextBlock, ...]) -> str:
    return "".join(block.text for block in blocks)


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True)
