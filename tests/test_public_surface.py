"""Public-import contract for the `runtime` package."""

import runtime


def test_exposes_provider_neutral_model_types() -> None:
    public_model_names = {
        "TextBlock",
        "SystemMessage",
        "UserMessage",
        "AssistantMessage",
        "ModelMessage",
        "ModelRequest",
        "ModelUsage",
        "TextDelta",
        "ModelCompletion",
        "ModelEvent",
        "ModelProvider",
        "JSONValue",
        "ToolSchema",
        "ToolCall",
        "ToolCallReady",
        "ToolResult",
        "ToolResultMessage",
    }
    for name in public_model_names:
        assert name in runtime.__all__, f"{name} missing from runtime.__all__"
        assert hasattr(runtime, name), f"runtime missing public attribute: {name}"


def test_exposes_official_provider_adapters() -> None:
    assert "OpenAICompatibleModelProvider" in runtime.__all__
    assert hasattr(runtime, "OpenAICompatibleModelProvider")
    assert "ScriptedModelProvider" in runtime.__all__
    assert hasattr(runtime, "ScriptedModelProvider")


def test_exposes_host_facing_runtime_lifecycle() -> None:
    assert "AgentRuntime" in runtime.__all__
    assert hasattr(runtime, "AgentRuntime")
    assert "RuntimeClosedError" in runtime.__all__
    assert hasattr(runtime, "RuntimeClosedError")


def test_keeps_runtime_internals_private() -> None:
    private_names = {"AgentLoop", "EnvironmentBinding", "EnvironmentKernel"}
    for name in private_names:
        assert name not in runtime.__all__
        assert not hasattr(runtime, name)


def test_all_entries_are_importable() -> None:
    for name in runtime.__all__:
        assert hasattr(runtime, name), f"runtime.__all__ entry not importable: {name}"
