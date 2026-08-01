import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from cli_agent.runtime import AgentRuntime, ScriptedModelProvider
from cli_agent.runtime._capability.mcp import catalog as mcp_catalog_module
from cli_agent.runtime._capability.mcp.catalog import _MCPCatalog
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

_FIXTURE = Path(__file__).parent / "mcp_server_fixture.py"


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    (repertoire / "_mcp").mkdir(parents=True, exist_ok=True)
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(exist_ok=True)
    return repertoire


def _server_config(repertoire: Path, name: str, command: list[str]) -> None:
    directory = repertoire / "_mcp" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(
            {
                "name": name,
                "transport": "stdio",
                "command": command,
                "env": ["FIXTURE_TOKEN"],
            }
        ),
        encoding="utf-8",
    )


def _open_view(workspace: Path, repertoire: Path) -> _CapabilityView:
    _prepare_workspace(workspace)
    return _CapabilityView.open(workspace, repertoire)


def _fixture_command() -> list[str]:
    return [sys.executable, str(_FIXTURE)]


def test_reconcile_generates_a_stub_with_mcp_prefix(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    view = _open_view(workspace, repertoire)
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        catalog = await _MCPCatalog.reconcile(view, on_diagnostic=received.append)
        assert received == []
        assert catalog.servers == ("math",)

        stub = view.root / "tools" / "mcp_math.py"
        assert stub.is_file()
        content = stub.read_text(encoding="utf-8")
        assert content.startswith('"""\nMCP Server (stdio): math')
        assert "def add(a: int, b: int):" in content
        assert "def say(text: str):" in content
        assert "return _call_mcp('add'," in content
        assert "_ENV_NAMES = ['FIXTURE_TOKEN']" in content
        assert "ghp_" not in content
        assert "FIXTURE_TOKEN=secret" not in content

    asyncio.run(scenario())


def test_reconcile_discovers_servers_in_parallel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    _server_config(repertoire, "echo", _fixture_command())
    view = _open_view(workspace, repertoire)

    entered = 0
    gate = asyncio.Event()
    original = mcp_catalog_module._discover

    async def gated_discover(config, on_diagnostic) -> object:
        nonlocal entered
        entered += 1
        if entered == 1:
            await asyncio.wait_for(gate.wait(), timeout=2)
        else:
            gate.set()
        return await original(config, on_diagnostic)

    monkeypatch.setattr(mcp_catalog_module, "_discover", gated_discover)

    async def scenario() -> None:
        catalog = await _MCPCatalog.reconcile(view)
        assert entered == 2
        assert catalog.servers == ("echo", "math")

    asyncio.run(scenario())


def test_reconcile_recontacts_servers_every_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    view = _open_view(workspace, repertoire)

    calls = 0
    original = mcp_catalog_module._list_tools_once

    async def counting_discover(config) -> object:
        nonlocal calls
        calls += 1
        return await original(config)

    monkeypatch.setattr(mcp_catalog_module, "_list_tools_once", counting_discover)

    async def scenario() -> None:
        await _MCPCatalog.reconcile(view)
        assert calls == 1
        first_stub = (view.root / "tools" / "mcp_math.py").read_bytes()

        second = await _MCPCatalog.reconcile(view)
        assert calls == 2
        assert (view.root / "tools" / "mcp_math.py").read_bytes() == first_stub
        assert second.servers == ("math",)

    asyncio.run(scenario())


def test_reconcile_retries_discovery_and_emits_diagnostic(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(
        repertoire,
        "broken",
        [sys.executable, str(tmp_path / "missing_server.py")],
    )
    view = _open_view(workspace, repertoire)
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        catalog = await _MCPCatalog.reconcile(view, on_diagnostic=received.append)

        assert catalog.servers == ()
        assert not (view.root / "tools" / "mcp_broken.py").exists()

        failures = [
            diagnostic
            for diagnostic in received
            if diagnostic.kind == "mcp.discovery_failed"
        ]
        assert len(failures) == 1
        assert failures[0].detail.get("server") == "broken"

    asyncio.run(scenario())


def test_reconcile_skips_invalid_config_with_diagnostic(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    directory = repertoire / "_mcp" / "bad"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps({"name": "bad", "transport": "stdio"}),
        encoding="utf-8",
    )
    view = _open_view(workspace, repertoire)
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        catalog = await _MCPCatalog.reconcile(view, on_diagnostic=received.append)

        assert catalog.servers == ()
        assert not (view.root / "tools" / "mcp_bad.py").exists()
        kinds = {diagnostic.kind for diagnostic in received}
        assert "mcp.config_invalid" in kinds

    asyncio.run(scenario())


def test_reconcile_removes_stub_for_a_removed_description(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    view = _open_view(workspace, repertoire)

    async def scenario() -> None:
        await _MCPCatalog.reconcile(view)
        stub = view.root / "tools" / "mcp_math.py"
        assert stub.is_file()

        shutil.rmtree(repertoire / "_mcp" / "math")
        second = await _MCPCatalog.reconcile(view)

        assert not stub.exists()
        assert second.servers == ()

    asyncio.run(scenario())


def test_reconcile_overwrites_a_locally_modified_stub(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    view = _open_view(workspace, repertoire)
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        await _MCPCatalog.reconcile(view)
        stub = view.root / "tools" / "mcp_math.py"
        stub.write_text("# user-edited\n", encoding="utf-8")

        catalog = await _MCPCatalog.reconcile(
            view, on_diagnostic=received.append
        )

        assert stub.read_text(encoding="utf-8").startswith(
            '"""\nMCP Server (stdio): math'
        )
        assert "# user-edited" not in stub.read_text(encoding="utf-8")
        assert catalog.servers == ("math",)
        assert not any(
            diagnostic.kind == "mcp.stub_modified"
            for diagnostic in received
        )

    asyncio.run(scenario())


def test_reconcile_never_touches_non_prefix_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    view = _open_view(workspace, repertoire)

    async def scenario() -> None:
        keep = view.root / "tools" / "keep.py"
        keep.write_text("# user tool\n", encoding="utf-8")

        await _MCPCatalog.reconcile(view)

        assert keep.read_text(encoding="utf-8") == "# user tool\n"
        assert (view.root / "tools" / "mcp_math.py").is_file()

    asyncio.run(scenario())


def test_runtime_open_projects_mcp_stub_without_diagnostics(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        async with await AgentRuntime.open(
            workspace=workspace,
            repertoire=repertoire,
            provider=ScriptedModelProvider(script=()),
            on_diagnostic=received.append,
        ) as runtime:
            stub = workspace / ".workspace" / "tools" / "mcp_math.py"
            assert stub.is_file()
            assert received == []
            assert runtime._mcp_catalog.servers == ("math",)

    asyncio.run(scenario())


def test_discovery_failure_keeps_runtime_open_without_partial_stub(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(
        repertoire,
        "broken",
        [sys.executable, str(tmp_path / "missing_server.py")],
    )
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        async with await AgentRuntime.open(
            workspace=workspace,
            repertoire=repertoire,
            provider=ScriptedModelProvider(script=()),
            on_diagnostic=received.append,
        ) as runtime:
            assert not (
                workspace / ".workspace" / "tools" / "mcp_broken.py"
            ).exists()
            assert runtime._mcp_catalog.servers == ()
            assert any(
                diagnostic.kind == "mcp.discovery_failed"
                for diagnostic in received
            )

    asyncio.run(scenario())


def test_generated_stub_is_valid_python_and_callable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    view = _open_view(workspace, repertoire)

    asyncio.run(_MCPCatalog.reconcile(view))

    stub_path = view.root / "tools" / "mcp_math.py"
    spec = importlib.util.spec_from_file_location("cli_agent_math", stub_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.add(1, 2) == "3"
    assert module.say("hello") == "hello"
