"""Provider-neutral model messages and requests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from runtime._builtin_tools import BUILDIN_TOOL_SCHEMA_DEFINITIONS, ToolSchema


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


@dataclass(frozen=True, slots=True)
class TextBlock:
    """A text block in a model message."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A fully assembled model Tool Call with decoded arguments."""

    call_id: str
    name: str
    arguments: dict[str, JSONValue]


@dataclass(frozen=True, slots=True)
class UserMessage:
    """A user-authored model message."""

    content: tuple[TextBlock, ...]

    @classmethod
    def text(cls, text: str) -> UserMessage:
        return cls(content=(TextBlock(text=text),))


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """A model-authored message retaining ordered text and tool call content."""

    content: tuple[TextBlock | ToolCall, ...]

    @classmethod
    def text(cls, text: str) -> AssistantMessage:
        return cls(content=(TextBlock(text=text),))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result of a Tool Call, associated with its originating call_id.

    Set ``output`` for a successful result; set ``error`` for a failed one.
    The other field stays at its default ``None``.
    """

    call_id: str
    output: JSONValue = None
    error: JSONValue = None


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    """A message carrying one or more Tool Results back to the model."""

    content: tuple[ToolResult, ...]


ModelMessage: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """The provider-neutral conversation and fixed environment protocol."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSchema, ...] = field(
        default=BUILDIN_TOOL_SCHEMA_DEFINITIONS,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class TextDelta:
    """An incremental piece of assistant text."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallReady:
    """A fully assembled Tool Call yielded by a provider before completion."""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """The terminal event for a successful text generation."""

    message: AssistantMessage
    finish_reason: str


ModelEvent: TypeAlias = TextDelta | ToolCallReady | ModelCompletion


class ModelProvider(Protocol):
    """A provider Adapter at the Agent Loop's model seam."""

    def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Generate one asynchronous stream of provider-neutral events."""

        ...
