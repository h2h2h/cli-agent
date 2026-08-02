from pathlib import Path

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
        "route.lane",
    )

    for symbol in legacy_symbols:
        assert symbol not in source, f"legacy Runtime symbol remains: {symbol}"
