"""Provider-neutral building blocks for the cli-agent Runtime."""

from cli_agent.runtime._builtin_tools import ToolSchema
from cli_agent.runtime._environment.command_parser import (
    CommandParseResult,
    ToolCommand,
    ToolReference,
)
from cli_agent.runtime._environment.policy import (
    ApprovalResponse,
    ExecutablePolicy,
    ExecutionApprovalRequest,
    ExecutionApprover,
    ExecutionPolicy,
    PolicyAction,
    PolicyEvaluation,
)
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
    "ApprovalResponse",
    "AssistantMessage",
    "CommandParseResult",
    "ExecutablePolicy",
    "ExecutionApprover",
    "ExecutionApprovalRequest",
    "ExecutionPolicy",
    "JSONValue",
    "ModelCompletion",
    "ModelEvent",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "ModelUsage",
    "OpenAICompatibleModelProvider",
    "PolicyAction",
    "PolicyEvaluation",
    "RuntimeClosedError",
    "ScriptedModelProvider",
    "SystemMessage",
    "ToolSchema",
    "TextBlock",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "ToolCommand",
    "ToolReference",
    "ToolResult",
    "ToolResultMessage",
    "UserMessage",
)
