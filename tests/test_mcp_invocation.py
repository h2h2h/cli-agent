import asyncio
import json
import sys
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime as runtime_module
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelRequest,
    ScriptedModelProvider,
    ToolCall,
    ToolResult,
    UserMessage,
)
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalCapabilityView,
)
from cli_agent.runtime._backend.local.deployment import _LocalCapabilityDeployment
from cli_agent.runtime._capability.mcp.config import discover_configs
from cli_agent.runtime._capability.projections import write_tool_index
from cli_agent.runtime._capability.provider import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._workspace import _LocalWorkspace

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)

_WORKSPACE_ID = "local:00000000000000000000000000000000"


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


def _fixture_command() -> list[str]:
    return [sys.executable, str(_FIXTURE)]


@pytest.mark.live_sync
async def _kernel(workspace: Path, repertoire: Path) -> EnvironmentKernel:
    _prepare_workspace(workspace)
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(workspace, {}, view)
    deployment = _LocalCapabilityDeployment(
        state_root=workspace / ".workspace",
        repertoire=repertoire,
        volume=".workspace",
        base_environment=backend.execution_base_environment,
    )
    opened = _LocalWorkspace(_WORKSPACE_ID, workspace, backend, repertoire)
    configs = await discover_configs(view)
    facts = await deployment.discover_mcp(configs)
    await deployment.materialize_stubs(opened, configs, facts)
    catalog = await _reconcile_tools(view, backend.filesystem)
    snapshot = CapabilitySnapshot(
        revision="test-revision",
        schema_version=CAPABILITY_SCHEMA_VERSION,
        tools=catalog,
        skills=await _SkillCatalog.discover(view),
        mcp_servers=configs,
        project_instructions=None,
    )
    deployed = await deployment.reconcile(snapshot, opened)
    assert deployed.complete, deployed.error
    return EnvironmentKernel(
        workspace,
        backend=backend,
        tool_catalog=catalog,
        tool_executor=deployment.executor(opened, revision=snapshot.revision),
    )


@pytest.mark.live_sync
def test_generated_mcp_tool_runs_through_tools_run(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())

    async def scenario() -> None:
        kernel = await _kernel(workspace, repertoire)
        try:
            added = _output(await _exec(kernel, 'tools run "tools.mcp_math.add(2, 3)"'))
            assert added["status"] == "exited"
            assert _text(added, "stdout") == "5\n"

            echoed = _output(
                await _exec(kernel, "tools run \"tools.mcp_math.say('hi')\"")
            )
            assert echoed["status"] == "exited"
            assert _text(echoed, "stdout") == "hi\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.live_sync
def test_local_and_mcp_tools_mix_in_one_code_block(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    (repertoire / "tools" / "local_tool.py").write_text(
        "def double(value):\n    return int(value) * 2\n"
    )

    async def scenario() -> None:
        kernel = await _kernel(workspace, repertoire)
        try:
            result = _output(
                await _exec(
                    kernel,
                    ('tools run "tools.local_tool.double(tools.mcp_math.add(1, 2))"'),
                )
            )
            assert result["status"] == "exited"
            assert _text(result, "stdout") == "6\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.live_sync
def test_mcp_connection_failure_returns_failed_result_without_deleting_stub(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())

    async def scenario() -> None:
        kernel = await _kernel(workspace, repertoire)
        try:
            stub = workspace / ".workspace" / "tools" / "mcp_math.py"
            assert stub.is_file()

            fixture = Path(_FIXTURE)
            backup = tmp_path / "server_fixture.py"
            fixture.rename(backup)
            try:
                failed = _output(
                    await _exec(kernel, 'tools run "tools.mcp_math.add(1, 2)"')
                )
                assert failed["status"] == "failed"
                assert _text(failed, "stderr")
            finally:
                backup.rename(fixture)

            assert stub.is_file()
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_mcp_integration_keeps_model_visible_surface_and_public_exports(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())
    received: list[object] = []

    async def scenario() -> None:
        async with await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=workspace,
            repertoire=repertoire,
            provider=ScriptedModelProvider(script=()),
            on_diagnostic=received.append,
            context_policy=_context_policy,
        ) as runtime:
            assert (workspace / ".workspace" / "tools" / "mcp_math.py").is_file()
            assert received == []
            mcp_tool = runtime._resources.snapshot.tools.get("mcp_math")
            assert mcp_tool is not None
            assert mcp_tool.parallel_safe is False

            request = ModelRequest(messages=())
            assert tuple(schema.name for schema in request.tools) == (
                "exec",
                "output",
                "kill",
            )
            assert not {
                name
                for name in ("mcp", "MCP", "_MCPCatalog")
                if name in runtime_module.__all__
            }

    asyncio.run(scenario())


def test_additional_sessions_do_not_reconcile_mcp_again(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    _server_config(repertoire, "math", _fixture_command())

    calls = 0
    original = _LocalCapabilityDeployment.discover_mcp

    async def counting_discover(self, configs, on_diagnostic=None):
        nonlocal calls
        calls += 1
        return await original(self, configs, on_diagnostic)

    monkeypatch.setattr(_LocalCapabilityDeployment, "discover_mcp", counting_discover)

    provider = ScriptedModelProvider(
        script=(
            (
                ModelCompletion(
                    message=AssistantMessage.text("ok"),
                    finish_reason="stop",
                ),
            ),
            (
                ModelCompletion(
                    message=AssistantMessage.text("ok"),
                    finish_reason="stop",
                ),
            ),
        )
    )

    async def scenario() -> None:
        async with await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=workspace,
            repertoire=repertoire,
            provider=provider,
            context_policy=_context_policy,
        ) as runtime:
            assert calls == 1
            await runtime.new_session()
            async for _ in runtime.run_turn(UserMessage.text("hello")):
                pass
            async for _ in runtime.run_turn(UserMessage.text("hello")):
                pass
            assert calls == 1
            provider.assert_exhausted()

    asyncio.run(scenario())


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
) -> ToolResult:
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(command)}",
            name="exec",
            arguments={"command": command, "wait_ms": 8_000},
        )
    )


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _text(snapshot: dict[str, object], stream: str) -> str:
    chunks = snapshot["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == stream
    )


async def _reconcile_tools(view, filesystem, on_diagnostic=None):
    catalog = await _ToolCatalog.discover(view, on_diagnostic)
    await write_tool_index(volume=view.root, filesystem=filesystem, catalog=catalog)
    return catalog
