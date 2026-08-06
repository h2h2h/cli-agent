"""Issue 09: Workspace MCP runs inside the Backend Workspace.

These tests pin the RFC-0012 issue 09 acceptance criteria: MCP discovery and
invocation both go through the Backend Workspace, the Runtime and Catalog
never create Workspace MCP subprocesses, credentials never enter stubs,
bindings, logs, or diagnostics, and MCP Tools keep flowing through the
ordinary Tool Catalog lifecycle and scheduling.
"""

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

from cli_agent.runtime._backend import mcp_runtime as mcp_runtime_module
from cli_agent.runtime._backend.facts import (
    _MCPServerFacts,
    _MCPToolFacts,
)
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalCapabilityView,
)
from cli_agent.runtime._capability.mcp.catalog import _MCPCatalog
from cli_agent.runtime._capability.mcp.facts import parse_server_config
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
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


def _open_backend(workspace: Path, repertoire: Path) -> _LocalBackendWorkspace:
    _prepare_workspace(workspace)
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    return _LocalBackendWorkspace(workspace, {}, view)


def _fixture_command() -> list[str]:
    return [sys.executable, str(_FIXTURE)]


def _math_config() -> object:
    config, errors = parse_server_config(
        {
            "name": "math",
            "transport": "stdio",
            "command": _fixture_command(),
            "env": ["FIXTURE_TOKEN"],
        },
        directory_name="math",
    )
    assert errors == ()
    return config


def test_discovery_runs_through_the_backend_runtime(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    backend = _open_backend(workspace, repertoire)

    async def scenario() -> None:
        facts = await backend.mcp.discover((_math_config(),))

        assert len(facts) == 1
        fact = facts[0]
        assert isinstance(fact, _MCPServerFacts)
        assert fact.name == "math"
        assert {tool.name for tool in fact.tools} == {"add", "say"}
        assert all(isinstance(tool, _MCPToolFacts) for tool in fact.tools)
        assert fact.tools[0].input_schema

    asyncio.run(scenario())


def test_discovery_resolves_env_values_only_from_the_base_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _prepare_workspace(workspace)
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(
        workspace,
        {"FIXTURE_TOKEN": "workspace-token"},
        view,
    )
    monkeypatch.setenv("FIXTURE_TOKEN", "host-token")

    async def record_list(config, base_environment) -> list[dict[str, object]]:
        assert config.name == "math"
        assert base_environment["FIXTURE_TOKEN"] == "workspace-token"
        return [{"name": "echo", "description": "", "input_schema": {}}]

    monkeypatch.setattr(mcp_runtime_module, "_list_tools_once", record_list)

    async def scenario() -> None:
        facts = await backend.mcp.discover((_math_config(),))
        assert [tool.name for tool in facts[0].tools] == ["echo"]

    asyncio.run(scenario())


def test_http_server_discovery_and_binding_are_backend_composed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    directory = repertoire / "_mcp" / "weather"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(
            {
                "name": "weather",
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "WEATHER_TOKEN"},
            }
        ),
        encoding="utf-8",
    )
    backend = _open_backend(workspace, repertoire)

    async def fake_list(config, base_environment) -> list[dict[str, object]]:
        del base_environment
        assert config.transport == "http"
        assert config.url == "https://example.com/mcp"
        return [
            {
                "name": "get_weather",
                "description": "Current weather.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

    monkeypatch.setattr(mcp_runtime_module, "_list_tools_once", fake_list)

    async def scenario() -> None:
        catalog = await _MCPCatalog.reconcile(backend)
        assert catalog.servers == ("weather",)

        stub = (Path(backend.capabilities.root) / "tools" / "mcp_weather.py").read_text(
            encoding="utf-8"
        )
        assert "MCP Server (http): weather" in stub
        assert "https://example.com/mcp" not in stub
        assert "WEATHER_TOKEN" not in stub
        assert "from mcp_binding import call_tool" in stub

        binding = (
            Path(backend.capabilities.root) / ".tool-environment" / "mcp_binding.py"
        ).read_text(encoding="utf-8")
        assert "https://example.com/mcp" in binding
        assert "WEATHER_TOKEN" in binding
        assert "get_weather" not in binding

    asyncio.run(scenario())


def test_binding_is_materialized_into_the_tool_runtime(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    backend = _open_backend(workspace, repertoire)

    async def scenario() -> None:
        await _MCPCatalog.reconcile(backend)

        binding_path = (
            Path(backend.capabilities.root) / ".tool-environment" / "mcp_binding.py"
        )
        assert binding_path.is_file()
        content = binding_path.read_text(encoding="utf-8")
        assert "_SERVERS = {" in content
        assert "'math': {'transport': 'stdio'" in content
        assert repr(_fixture_command()[0]) in content
        assert "def call_tool(name, tool_name, arguments):" in content
        assert "asyncio.run(_async_call_tool" in content

    asyncio.run(scenario())


def test_credentials_never_enter_stubs_binding_or_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    _server_config(
        repertoire,
        "broken",
        [sys.executable, str(tmp_path / "missing_server.py")],
    )
    backend = _open_backend(workspace, repertoire)
    monkeypatch.setenv("FIXTURE_TOKEN", "super-secret-token")
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        await _MCPCatalog.reconcile(backend, on_diagnostic=received.append)

        stub = (Path(backend.capabilities.root) / "tools" / "mcp_math.py").read_text(
            encoding="utf-8"
        )
        assert "super-secret-token" not in stub
        assert "FIXTURE_TOKEN" not in stub

        binding = (
            Path(backend.capabilities.root) / ".tool-environment" / "mcp_binding.py"
        ).read_text(encoding="utf-8")
        assert "FIXTURE_TOKEN" in binding
        assert "super-secret-token" not in binding

        assert any(diagnostic.kind == "mcp.discovery_failed" for diagnostic in received)
        for diagnostic in received:
            assert "super-secret-token" not in repr(diagnostic)

    asyncio.run(scenario())


def test_mcp_tools_flow_through_the_ordinary_tool_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    backend = _open_backend(workspace, repertoire)

    async def scenario() -> None:
        await _MCPCatalog.reconcile(backend)
        catalog = await _ToolCatalog.reconcile(backend.capabilities, backend.filesystem)

        entry = catalog.get("mcp_math")
        assert entry is not None
        assert entry.valid
        assert entry.provenance == "workspace"
        assert entry.parallel_safe is False
        assert "MCP Server (stdio): math" in (entry.documentation or "")

    asyncio.run(scenario())


def _package_modules(package: str) -> tuple[Path, ...]:
    package_path = Path(importlib.import_module(package).__file__).parent
    return tuple(path for path in package_path.rglob("*.py"))


def test_mcp_catalog_and_runtime_never_create_workspace_mcp_subprocesses() -> None:
    for path in _package_modules("cli_agent.runtime._capability.mcp"):
        source = path.read_text(encoding="utf-8")
        assert "create_subprocess" not in source, path
        assert "from mcp import" not in source, path
        assert "import mcp" not in source, path
        assert "stdio_client" not in source, path
        assert "streamable_http_client" not in source, path
        assert "httpx" not in source, path

    resources_source = Path(
        importlib.import_module("cli_agent.runtime._resources").__file__
    ).read_text(encoding="utf-8")
    assert "stdio_client" not in resources_source
    assert "ClientSession" not in resources_source

    local_backend = Path(
        importlib.import_module("cli_agent.runtime._backend.local").__file__
    ).read_text(encoding="utf-8")
    assert "create_subprocess_shell" in local_backend
    assert "create_subprocess_exec" in local_backend


def test_runtime_open_uses_the_backend_mcp_runtime(tmp_path: Path) -> None:
    from interaction_fakes import _ScriptedInteraction

    from cli_agent.runtime import (
        AgentRuntime,
        ContextPolicy,
        ScriptedModelProvider,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())

    async def scenario() -> None:
        async with await AgentRuntime.open(
            user_interaction=_ScriptedInteraction("allow_once"),
            workspace=workspace,
            repertoire=repertoire,
            provider=ScriptedModelProvider(script=()),
            context_policy=ContextPolicy(
                context_window_tokens=16_384,
                output_reserve_tokens=2_048,
                safety_margin_tokens=0,
            ),
        ) as runtime:
            binding = workspace / ".workspace" / ".tool-environment" / "mcp_binding.py"
            assert binding.is_file()
            assert "'math':" in binding.read_text(encoding="utf-8")
            tool = runtime._resources.tool_catalog.get("mcp_math")
            assert tool is not None and tool.valid

    asyncio.run(scenario())
