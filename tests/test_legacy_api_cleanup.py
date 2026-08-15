from pathlib import Path

from cli_agent.runtime._backend import _BackendWorkspace
from cli_agent.runtime._backend import protocol as backend_protocol

_RUNTIME_SOURCE = Path(__file__).parents[1] / "src" / "cli_agent" / "runtime"


def test_runtime_source_has_no_legacy_routing_apis() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _RUNTIME_SOURCE.rglob("*.py")
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
    for member in ("capabilities", "mcp", "reconcile_tool_runtime"):
        assert not hasattr(_BackendWorkspace, member)

    protocol_types = {
        name for name, value in vars(backend_protocol).items() if isinstance(value, type)
    }
    assert "_BoundCapabilityView" not in protocol_types
    assert "_WorkspaceMCPRuntime" not in protocol_types

    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (_RUNTIME_SOURCE / "_backend").rglob("*.py")
        if "local" not in str(path)
    )
    for symbol in (
        "materialize_binding",
        "_MCPServerFacts",
        "_MCPToolFacts",
        "_ToolRuntimeStatus",
        "_CapabilitySource",
        "_CapabilityState",
    ):
        assert symbol not in source, f"legacy Backend capability symbol remains: {symbol}"
