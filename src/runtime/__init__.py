"""Provider-neutral building blocks for an embeddable agent runtime."""

from runtime._syscalls import SyscallSchema
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
    "SyscallSchema",
    "TextBlock",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "ToolResult",
    "ToolResultMessage",
    "UserMessage",
)
