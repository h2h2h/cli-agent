import importlib
import inspect
from pathlib import Path

from cli_agent.runtime._backend import _BackendWorkspace
from cli_agent.runtime._backend import protocol as backend_protocol
from cli_agent.runtime.runtime import AgentRuntime

_RUNTIME_SOURCE = Path(__file__).parents[1] / "src" / "cli_agent" / "runtime"


def test_runtime_source_has_no_legacy_routing_apis() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in _RUNTIME_SOURCE.rglob("*.py")
    )
    legacy_symbols = (
        "_DriverKind",
        "_SchedulingClass",
        "_ExecutionLane",
        "_ToolDriver",
        "parallel_tools",
        "tool_parallel_limit",
        "command.tool",
        "route.lane",
    )

    for symbol in legacy_symbols:
        assert symbol not in source, f"legacy Runtime symbol remains: {symbol}"

    assert not (_RUNTIME_SOURCE / "_environment" / "drivers").exists()


def test_backend_contract_has_no_capability_plane_members() -> None:
    for member in ("capabilities", "mcp", "reconcile_tool_runtime", "prepare_tool"):
        assert not hasattr(_BackendWorkspace, member)

    protocol_types = {
        name
        for name, value in vars(backend_protocol).items()
        if isinstance(value, type)
    }
    assert "_BoundCapabilityView" not in protocol_types
    assert "_WorkspaceMCPRuntime" not in protocol_types
    assert "_ToolExecutor" not in protocol_types

    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (_RUNTIME_SOURCE / "_backend").rglob("*.py")
        if "local" not in str(path) and path.name != "deployment.py"
    )
    for symbol in (
        "materialize_binding",
        "_MCPServerFacts",
        "_MCPToolFacts",
        "_ToolRuntimeStatus",
        "_CapabilitySource",
        "_CapabilityState",
    ):
        assert symbol not in source, (
            f"legacy Backend capability symbol remains: {symbol}"
        )


def test_backend_contract_has_no_tool_execution_members() -> None:
    protocol_source = Path(backend_protocol.__file__).read_text(encoding="utf-8")
    for symbol in ("prepare_tool", "_ToolExecutionRequest", "worker", "venv"):
        assert symbol not in protocol_source, (
            f"legacy Backend Tool member remains: {symbol}"
        )

    local_backend_source = Path(
        importlib.import_module("cli_agent.runtime._backend.local.backend").__file__
    ).read_text(encoding="utf-8")
    assert "prepare_tool" not in local_backend_source


def test_runtime_has_a_single_active_session_api() -> None:
    runtime_source = Path(
        importlib.import_module("cli_agent.runtime.runtime").__file__
    ).read_text(encoding="utf-8")
    for symbol in ("_sessions", "close_session", "active_task"):
        assert symbol not in runtime_source, (
            f"legacy Runtime member remains: {symbol}"
        )

    run_turn_parameters = inspect.signature(AgentRuntime.run_turn).parameters
    assert "session_id" not in run_turn_parameters
    assert "provider" not in run_turn_parameters

    usage_parameters = inspect.signature(AgentRuntime.session_usage).parameters
    assert "session_id" not in usage_parameters

    assert not hasattr(AgentRuntime, "close_session")
    for method in (
        "new_session",
        "resume_session",
        "detach_session",
        "archive_session",
        "unarchive_session",
        "delete_session",
    ):
        assert hasattr(AgentRuntime, method)
