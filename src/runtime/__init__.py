"""Provider-neutral building blocks for an embeddable agent runtime."""

from runtime.model import (
    AssistantMessage,
    JSONValue,
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from runtime.providers import OpenAICompatibleModelProvider

__all__ = (
    "AssistantMessage",
    "JSONValue",
    "ModelCompletion",
    "ModelEvent",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "OpenAICompatibleModelProvider",
    "TextBlock",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "ToolResult",
    "ToolResultMessage",
    "UserMessage",
)
