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
    ModelUsage,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from runtime.providers import OpenAICompatibleModelProvider, ScriptedModelProvider
from runtime.runtime import AgentRuntime, RuntimeClosedError

__all__ = (
    "AgentRuntime",
    "AssistantMessage",
    "JSONValue",
    "ModelCompletion",
    "ModelEvent",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "ModelUsage",
    "OpenAICompatibleModelProvider",
    "RuntimeClosedError",
    "ScriptedModelProvider",
    "SystemMessage",
    "ToolSchema",
    "TextBlock",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "ToolResult",
    "ToolResultMessage",
    "UserMessage",
)
