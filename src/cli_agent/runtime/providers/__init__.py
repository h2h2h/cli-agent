"""Model Provider adapters."""

from cli_agent.runtime.providers.openai_compatible import (
    OpenAICompatibleModelProvider,
)
from cli_agent.runtime.providers.scripted import ScriptedModelProvider

__all__ = ["OpenAICompatibleModelProvider", "ScriptedModelProvider"]
