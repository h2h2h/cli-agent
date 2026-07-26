"""Model Provider adapters."""

from runtime.providers.openai_compatible import OpenAICompatibleModelProvider
from runtime.providers.scripted import ScriptedModelProvider

__all__ = ["OpenAICompatibleModelProvider", "ScriptedModelProvider"]
