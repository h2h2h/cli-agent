import asyncio
from pathlib import Path

import pytest

from cli_agent.runtime import AgentRuntime, RuntimeDiagnostic, ScriptedModelProvider


def test_diagnostic_is_frozen() -> None:
    diagnostic = RuntimeDiagnostic(kind="mcp.discovery_failed", message="boom")
    with pytest.raises(AttributeError):
        diagnostic.kind = "other"  # type: ignore[misc]


def test_emission_is_silent_without_a_callback(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )
        runtime._emit_diagnostic("mcp.discovery_failed", "boom")
        await runtime.close()

    asyncio.run(scenario())


def test_callback_receives_structured_diagnostics(tmp_path: Path) -> None:
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            on_diagnostic=received.append,
        )
        runtime._emit_diagnostic(
            "mcp.discovery_failed",
            "could not contact github",
            detail={"server": "github"},
        )
        await runtime.close()

    asyncio.run(scenario())

    assert received == [
        RuntimeDiagnostic(
            kind="mcp.discovery_failed",
            message="could not contact github",
            detail={"server": "github"},
        )
    ]


def test_callback_receives_tool_metadata_parse_diagnostic(tmp_path: Path) -> None:
    repertoire = tmp_path / "repertoire"
    (repertoire / "tools").mkdir(parents=True)
    for name in ("skills", "library"):
        (repertoire / name).mkdir()
    (repertoire / "tools" / "broken.py").write_text(
        "PARALLEL_SAFE = 'yes'\nVALUE = 1\n",
        encoding="utf-8",
    )
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            repertoire=repertoire,
            provider=ScriptedModelProvider(script=()),
            on_diagnostic=received.append,
        )
        await runtime.close()

    asyncio.run(scenario())

    assert len(received) == 1
    assert received[0].kind == "tools.parallel_safe_parse_failed"
    assert received[0].detail["tool"] == "broken"
    assert received[0].detail["default_parallel_safe"] is True
