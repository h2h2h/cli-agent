"""Provider-neutral model messages and requests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, TypeAlias


@dataclass(frozen=True, slots=True)
class TextBlock:
    """A text block in a model message."""

    text: str


@dataclass(frozen=True, slots=True)
class UserMessage:
    """A user-authored model message."""

    content: tuple[TextBlock, ...]

    @classmethod
    def text(cls, text: str) -> UserMessage:
        return cls(content=(TextBlock(text=text),))


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """A model-authored message."""

    content: tuple[TextBlock, ...]

    @classmethod
    def text(cls, text: str) -> AssistantMessage:
        return cls(content=(TextBlock(text=text),))


ModelMessage: TypeAlias = UserMessage | AssistantMessage


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """The provider-neutral conversation submitted for generation."""

    messages: tuple[ModelMessage, ...]


@dataclass(frozen=True, slots=True)
class TextDelta:
    """An incremental piece of assistant text."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """The terminal event for a successful text generation."""

    message: AssistantMessage
    finish_reason: str


ModelEvent: TypeAlias = TextDelta | ModelCompletion


class ModelProvider(Protocol):
    """A provider Adapter at the Agent Loop's model seam."""

    def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Generate one asynchronous stream of provider-neutral events."""

        ...
