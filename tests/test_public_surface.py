"""Public-import contract for the `runtime` package."""

import runtime


def test_exposes_provider_neutral_model_types() -> None:
    public_model_names = {
        "TextBlock",
        "UserMessage",
        "AssistantMessage",
        "ModelMessage",
        "ModelRequest",
        "TextDelta",
        "ModelCompletion",
        "ModelEvent",
        "ModelProvider",
    }
    for name in public_model_names:
        assert name in runtime.__all__, f"{name} missing from runtime.__all__"
        assert hasattr(runtime, name), f"runtime missing public attribute: {name}"


def test_exposes_official_provider_adapters() -> None:
    assert "OpenAICompatibleModelProvider" in runtime.__all__
    assert hasattr(runtime, "OpenAICompatibleModelProvider")


def test_keeps_agent_loop_private() -> None:
    assert "AgentLoop" not in runtime.__all__
    assert not hasattr(runtime, "AgentLoop")


def test_all_entries_are_importable() -> None:
    for name in runtime.__all__:
        assert hasattr(runtime, name), f"runtime.__all__ entry not importable: {name}"
