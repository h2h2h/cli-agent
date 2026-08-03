"""Provider-neutral building blocks for the cli-agent Runtime."""

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.interaction import (
    UserAnswer,
    UserInteraction,
    UserOption,
    UserQuestion,
)
from cli_agent.runtime._environment.policy import (
    ExecutionPolicy,
    PolicyAction,
    PolicyEvaluation,
)
from cli_agent.runtime._syscalls import SyscallSchema
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
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
    "ShellParseResult",
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
    "RuntimeDiagnostic",
    "ScriptedModelProvider",
    "SystemMessage",
    "SyscallSchema",
    "TextBlock",
    "TextDelta",
    "ToolCall",
    "ToolCallReady",
    "ToolResult",
    "ToolResultMessage",
    "UserAnswer",
    "UserInteraction",
    "UserMessage",
    "UserOption",
    "UserQuestion",
)
