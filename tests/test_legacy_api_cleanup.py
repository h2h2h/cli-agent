from pathlib import Path

_RUNTIME_SOURCE = Path(__file__).parents[1] / "src" / "cli_agent" / "runtime"
_ARCHITECTURE_DOC = Path(__file__).parents[1] / "docs" / "architecture.md"
_RFC_0003 = (
    Path(__file__).parents[1]
    / "docs"
    / "rfcs"
    / "approved"
    / "RFC-0003-tool-capability-commands.md"
)


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


def test_architecture_docs_pin_the_rfc_0007_execution_model() -> None:
    architecture = _ARCHITECTURE_DOC.read_text(encoding="utf-8")
    rfc_0003 = _RFC_0003.read_text(encoding="utf-8")

    assert "Custom registry + Shell fallback" in architecture
    assert "single pending queue + barriers" in architecture
    assert "no Tool-specific lane" in architecture
    assert "Superseded by [RFC-0007]" in rfc_0003
    assert "`CommandParseResult.tool`" in rfc_0003
