"""Public-import contract for the `cli_agent.runtime` package."""

from cli_agent import runtime


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
        "SyscallSchema",
        "ToolCall",
        "ToolCallReady",
        "ToolResult",
        "ToolResultMessage",
    }
    for name in public_model_names:
        assert name in runtime.__all__, f"{name} missing from cli_agent.runtime.__all__"
        assert hasattr(runtime, name), (
            f"cli_agent.runtime missing public attribute: {name}"
        )


def test_exposes_host_execution_policy_contracts() -> None:
    public_policy_names = {
        "CommandParseResult",
        "PolicyAction",
        "PolicyEvaluation",
        "ExecutablePolicy",
        "ExecutionPolicy",
        "ExecutionApprovalRequest",
        "ApprovalResponse",
        "ExecutionApprover",
        "ToolCommand",
    }
    for name in public_policy_names:
        assert name in runtime.__all__, f"{name} missing from cli_agent.runtime.__all__"
        assert hasattr(runtime, name), (
            f"cli_agent.runtime missing public attribute: {name}"
        )


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
    assert "RuntimeDiagnostic" in runtime.__all__
    assert hasattr(runtime, "RuntimeDiagnostic")


def test_keeps_runtime_internals_private() -> None:
    private_names = {"AgentLoop", "EnvironmentKernel"}
    for name in private_names:
        assert name not in runtime.__all__
        assert not hasattr(runtime, name)


def test_all_entries_are_importable() -> None:
    for name in runtime.__all__:
        assert hasattr(runtime, name), (
            f"cli_agent.runtime.__all__ entry not importable: {name}"
        )
