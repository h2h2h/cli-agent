"""Provider-neutral model messages and requests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from cli_agent.runtime._syscalls import (
    BUILT_IN_SYSCALL_SCHEMAS,
    SyscallSchema,
)

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
class SystemMessage:
    """A Runtime-authored model instruction."""

    content: tuple[TextBlock, ...]

    @classmethod
    def text(cls, text: str) -> SystemMessage:
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


ModelMessage: TypeAlias = (
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """The provider-neutral conversation and fixed environment protocol."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[SyscallSchema, ...] = field(
        default=BUILT_IN_SYSCALL_SCHEMAS,
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
class ModelUsage:
    """Provider-neutral token counts reported for one completion."""

    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.total_tokens) < 0:
            raise ValueError("model usage token counts must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """The terminal event for a successful text generation."""

    message: AssistantMessage
    finish_reason: str
    usage: ModelUsage | None = None


ModelEvent: TypeAlias = TextDelta | ToolCallReady | ModelCompletion


class ModelProvider(Protocol):
    """A provider Adapter at the Agent Loop's model seam."""

    def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Generate one asynchronous stream of provider-neutral events."""

        ...
