"""Provider-neutral building blocks for an embeddable agent runtime."""

from runtime._builtin_tools import ToolSchema
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
from runtime.providers import OpenAICompatibleModelProvider, ScriptedModelProvider

__all__ = (
    "AssistantMessage",
    "JSONValue",
    "ModelCompletion",
    "ModelEvent",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "OpenAICompatibleModelProvider",
    "ScriptedModelProvider",
    "ToolSchema",
    "TextBlock",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "ToolResult",
    "ToolResultMessage",
    "UserMessage",
)
