"""Provider-neutral building blocks for an embeddable agent runtime."""

from runtime.model import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    TextBlock,
    TextDelta,
    UserMessage,
)
from runtime.providers import OpenAICompatibleModelProvider

__all__ = (
    "AssistantMessage",
    "ModelCompletion",
    "ModelEvent",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "OpenAICompatibleModelProvider",
    "TextBlock",
    "TextDelta",
    "UserMessage",
)
