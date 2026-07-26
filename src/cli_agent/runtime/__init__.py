"""Provider-neutral building blocks for the cli-agent Runtime."""

from cli_agent.runtime._builtin_tools import ToolSchema
from cli_agent.runtime.model import (
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
from cli_agent.runtime.providers import (
    OpenAICompatibleModelProvider,
    ScriptedModelProvider,
)
from cli_agent.runtime.runtime import AgentRuntime, RuntimeClosedError

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
