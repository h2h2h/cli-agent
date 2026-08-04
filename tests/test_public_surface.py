"""Public-import contract for the `cli_agent.runtime` package."""

from pathlib import Path

from cli_agent import runtime


def _runtime_source_files() -> list[Path]:
    root = Path(runtime.__file__).parent
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


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
        "ContextPolicy",
        "ShellParseResult",
        "PolicyAction",
        "PolicyEvaluation",
        "ExecutionPolicy",
        "UserAnswer",
        "UserInteraction",
        "UserOption",
        "UserQuestion",
    }
    for name in public_policy_names:
        assert name in runtime.__all__, f"{name} missing from cli_agent.runtime.__all__"
        assert hasattr(runtime, name), (
            f"cli_agent.runtime missing public attribute: {name}"
        )


def test_removed_approver_types_are_not_public() -> None:
    removed_names = {
        "ApprovalResponse",
        "ExecutionApprovalRequest",
        "ExecutionApprover",
        "ExecutablePolicy",
        "ExecutionDecision",
        "_ExecutionApprovalGate",
    }
    for name in removed_names:
        assert name not in runtime.__all__, f"{name} must not be public"
        assert not hasattr(runtime, name), f"{name} must not be public"


def test_rejected_shared_semantics_are_absent_from_source() -> None:
    forbidden = (
        "shell_catalog",
        "ShellEffect",
        "CompositeFact",
    )
    for path in _runtime_source_files():
        source = path.read_text(encoding="utf-8")
        for term in forbidden:
            assert term not in source, f"{term} found in {path}"


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
    private_names = {
        "AgentLoop",
        "EnvironmentKernel",
        "ToolCommand",
        "ContextOperation",
        "ContextOverflowError",
        "ContextPressure",
        "PreparedContext",
        "_ContextLedger",
        "_ContextLedgerError",
        "_ContextManager",
        "_ToolResultReducer",
        "_RuntimeResources",
        "_reconcile_runtime_resources",
    }
    for name in private_names:
        assert name not in runtime.__all__
        assert not hasattr(runtime, name)


def test_all_entries_are_importable() -> None:
    for name in runtime.__all__:
        assert hasattr(runtime, name), (
            f"cli_agent.runtime.__all__ entry not importable: {name}"
        )
