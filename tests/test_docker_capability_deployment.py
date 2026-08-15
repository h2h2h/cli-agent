"""Issue 17: Docker CapabilityDeployment integrates the deployment plane.

These tests prove the RFC-0017 acceptance criteria against a real Docker
daemon: tool workers, dependencies, and MCP bindings live in the durable
capability volume and never depend on a temporary execution container; the
Docker ToolExecutor consumes only the DeploymentSnapshot and Workspace
primitives and its worker containers never mount Host paths; deployment
update / rollback / reopen semantics hold; and the Local / Docker
Kernel-visible tool, output, and kill semantics stay consistent. The
cases carry the ``docker`` integration marker and skip when no daemon is
reachable; the CI Docker job must run them against a real daemon.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ScriptedModelProvider,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    UserMessage,
)
from cli_agent.runtime._backend import _ToolBinding, _ToolExecutionRequest
from cli_agent.runtime._backend.docker import deployment as docker_deployment
from cli_agent.runtime._backend.docker.deployment import (
    _DockerCapabilityDeployment,
    _DockerCapabilityView,
    _DockerToolExecutor,
)
from cli_agent.runtime._backend.facts import _FileWriteRequest
from cli_agent.runtime._capability.deployment import (
    DEPLOYMENT_MANIFEST,
    DeploymentSnapshot,
    read_manifest,
    volume_path,
)
from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.provider import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.handlers.base import _CommandContext
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionOutputSink,
    ExitStatus,
)
from cli_agent.runtime._workspace import (
    _DockerWorkspace,
    _DockerWorkspaceFactory,
)

_IMAGE = "python:3.12-alpine"
_REVISION = "docker-revision"

pytestmark = pytest.mark.docker

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _require_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker daemon is unavailable")


def _prepare_project(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    state = project / ".workspace"
    state.mkdir()
    lines = [f"{key}={value}" for key, value in (env or {}).items()]
    (state / "env").write_text(
        ("\n".join(lines) + ("\n" if lines else "")),
        encoding="utf-8",
    )
    repertoire = tmp_path / "repertoire"
    for name in ("tools", "skills", "library", "_mcp"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return project, repertoire


async def _open(project: Path, repertoire: Path) -> _DockerWorkspace:
    return await _DockerWorkspaceFactory(image=_IMAGE).open(
        project,
        repertoire=repertoire,
    )


def _deployment(workspace) -> _DockerCapabilityDeployment:
    return _DockerCapabilityDeployment(
        state_root=workspace.state_root,
        repertoire=workspace.repertoire,
        volume=workspace.deployment_volume,
        backend=workspace.backend,
    )


def _snapshot(*, revision: str = _REVISION) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        revision=revision,
        schema_version=CAPABILITY_SCHEMA_VERSION,
        tools=_ToolCatalog(()),
        skills=_SkillCatalog(()),
        mcp_servers=(),
        project_instructions=None,
    )


async def _remove_volume(volume: str) -> None:
    try:
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _provision_tool(
    project: Path,
    repertoire: Path,
    *,
    tool_source: str = "VALUE = 1\n",
) -> None:
    (repertoire / "tools" / "math_tool.py").write_text(tool_source, encoding="utf-8")


def test_view_materializes_repertoire_and_preserves_overrides(tmp_path: Path) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)
    (repertoire / "tools" / "math.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repertoire / "skills" / "review").mkdir(parents=True, exist_ok=True)
    (repertoire / "skills" / "review" / "SKILL.md").write_text(
        "# Review\n",
        encoding="utf-8",
    )

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            view = await _DockerCapabilityView.materialize(
                workspace.backend,
                workspace.deployment_volume,
                repertoire,
            )
            assert await view.read("tools/math.py") == b"VALUE = 1\n"
            assert [entry.name for entry in await view.list("skills")] == ["review"]
            inspection = await view.inspect("tools/math.py")
            assert inspection.provenance == "repertoire"
            assert inspection.valid is True

            await workspace.filesystem.write(
                _FileWriteRequest(
                    path=".workspace/tools/math.py",
                    content=b"VALUE = 2\n",
                )
            )
            inspection = await view.inspect("tools/math.py")
            assert inspection.provenance == "workspace"
            assert inspection.shadows_repertoire is True

            (repertoire / "tools" / "math.py").write_text(
                "VALUE = 9\n",
                encoding="utf-8",
            )
            await view._sync(repertoire)
            assert await view.read("tools/math.py") == b"VALUE = 2\n"

            (repertoire / "tools" / "added.py").write_text(
                "X = 1\n",
                encoding="utf-8",
            )
            await view._sync(repertoire)
            assert await view.read("tools/added.py") == b"X = 1\n"

            (repertoire / "tools" / "added.py").unlink()
            await view._sync(repertoire)
            with pytest.raises(_FilesystemError):
                await view.read("tools/added.py")
            assert await view.read("tools/math.py") == b"VALUE = 2\n"
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_view_consumes_whiteout_markers(tmp_path: Path) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)
    (repertoire / "tools" / "hidden.py").write_text("H = 1\n", encoding="utf-8")

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            view = await _DockerCapabilityView.materialize(
                workspace.backend,
                workspace.deployment_volume,
                repertoire,
            )
            assert await view.read("tools/hidden.py") == b"H = 1\n"

            await workspace.filesystem.write(
                _FileWriteRequest(
                    path=".workspace/.capability-view/whiteouts/tools/hidden.py",
                    content=b"x",
                )
            )
            await view._sync(repertoire)
            inspection = await view.inspect("tools/hidden.py")
            assert inspection.provenance == "whiteout"
            with pytest.raises(_FilesystemError):
                await view.read("tools/hidden.py")
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_reconcile_publishes_artifacts_and_records_mount_contract(
    tmp_path: Path,
) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            deployment = _deployment(workspace)
            result = await deployment.reconcile(_snapshot(), workspace)
            assert result.complete, result.error
            assert result.workspace_id == workspace.id
            assert result.revision == _REVISION
            assert result.mounts == (workspace.volume,)

            filesystem = workspace.backend.filesystem
            manifest = await read_manifest(
                filesystem,
                volume_path(workspace.deployment_volume, DEPLOYMENT_MANIFEST),
            )
            assert manifest is not None
            assert manifest.complete
            assert manifest.workspace_id == workspace.id

            worker = await filesystem.read(
                ".workspace/.tool-environment/worker.py",
            )
            assert b"def main()" in worker
            python = await filesystem.stat(
                ".workspace/.tool-environment/.venv/bin/python",
            )
            assert python.kind == "file"
            marker = await filesystem.read(
                ".workspace/.tool-environment/requirements.sha256",
            )
            assert len(marker.decode("ascii").strip()) == 64
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_reconcile_reuses_the_deployment_after_reopen(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        first = await _deployment(workspace).reconcile(_snapshot(), workspace)
        assert first.complete, first.error
        await workspace.close()

        async def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("setup must not run when the deployment is current")

        monkeypatch.setattr(docker_deployment, "_run_docker_setup", forbidden)

        reopened = await _open(project, repertoire)
        try:
            second = await _deployment(reopened).reconcile(_snapshot(), reopened)
            assert second.complete, second.error
            assert second.mounts == (reopened.volume,)
            runtime = reopened.backend._tool_runtime
            assert runtime is not None and runtime.available
        finally:
            await reopened.close()
            await _remove_volume(reopened.volume)

    asyncio.run(scenario())


def test_failed_setup_keeps_the_previous_manifest_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            first = await _deployment(workspace).reconcile(_snapshot(), workspace)
            assert first.complete, first.error

            original = docker_deployment._run_docker_setup

            async def fail_setup(*args: object, **kwargs: object) -> tuple[int, str]:
                del args, kwargs
                return 1, "forced dependency failure"

            monkeypatch.setattr(docker_deployment, "_run_docker_setup", fail_setup)

            await workspace.filesystem.write(
                _FileWriteRequest(
                    path=".workspace/tools/requirements.txt",
                    content=b"definitely-not-a-real-package-xyz==1\n",
                )
            )
            failed = await _deployment(workspace).reconcile(_snapshot(), workspace)
            assert not failed.complete
            assert failed.error.startswith("Tool environment is unavailable")

            manifest = await read_manifest(
                workspace.backend.filesystem,
                volume_path(workspace.deployment_volume, DEPLOYMENT_MANIFEST),
            )
            assert manifest is not None
            assert manifest.complete
            assert manifest.revision == _REVISION

            monkeypatch.setattr(docker_deployment, "_run_docker_setup", original)

            await workspace.filesystem.write(
                _FileWriteRequest(
                    path=".workspace/tools/requirements.txt",
                    content=b"",
                )
            )
            recovered = await _deployment(workspace).reconcile(
                _snapshot(),
                workspace,
            )
            assert recovered.complete, recovered.error
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_stale_and_foreign_deployments_fail_classified(tmp_path: Path) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)
    _provision_tool(project, repertoire)

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            deployment = _deployment(workspace)
            result = await deployment.reconcile(_snapshot(), workspace)
            assert result.complete, result.error

            executor = deployment.executor(workspace, revision="other-revision")
            execution = executor.prepare(
                _ToolExecutionRequest(
                    code="tools.math_tool.VALUE",
                    bindings=(),
                ),
                _CommandContext(
                    workspace=workspace.root,
                    cwd="/workspace",
                    environment={},
                ),
            )
            assert isinstance(execution, _InlineExecution)

            foreign = _DockerToolExecutor(
                workspace.backend,
                workspace_id="docker:" + "f" * 32,
                revision=_REVISION,
                deployment=DeploymentSnapshot(
                    workspace_id="docker:" + "f" * 32,
                    revision=_REVISION,
                    layout_version=1,
                    complete=False,
                    error="Tool environment is unavailable: foreign",
                ),
                runtime=None,
            )
            execution = foreign.prepare(
                _ToolExecutionRequest(code="print(1)", bindings=()),
                _CommandContext(
                    workspace=workspace.root,
                    cwd="/workspace",
                    environment={},
                ),
            )
            assert isinstance(execution, _InlineExecution)
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_invalid_binding_fails_classified(tmp_path: Path) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            deployment = _deployment(workspace)
            result = await deployment.reconcile(_snapshot(), workspace)
            assert result.complete, result.error

            executor = deployment.executor(workspace, revision=_REVISION)
            execution = executor.prepare(
                _ToolExecutionRequest(
                    code="print(1)",
                    bindings=(
                        _ToolBinding(
                            name="escape",
                            path="../etc/passwd",
                        ),
                    ),
                ),
                _CommandContext(
                    workspace=workspace.root,
                    cwd="/workspace",
                    environment={},
                ),
            )
            assert isinstance(execution, _InlineExecution)
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_concurrent_reconciles_serialize_one_setup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)
    counter = {"setup_runs": 0}
    original = docker_deployment._run_docker_setup

    async def counted(*args: object, **kwargs: object) -> tuple[int, str]:
        counter["setup_runs"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(docker_deployment, "_run_docker_setup", counted)

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            first = _deployment(workspace)
            second = _deployment(workspace)
            results = await asyncio.gather(
                first.reconcile(_snapshot(), workspace),
                second.reconcile(_snapshot(), workspace),
            )
            assert all(result.complete for result in results)
            assert counter["setup_runs"] == 1
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_tool_worker_execution_semantics_match_local(tmp_path: Path) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path, env={"WS_KEY": "from-workspace"})
    (repertoire / "tools" / "env_tool.py").write_text("VALUE = 1\n", encoding="utf-8")

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            view = await _DockerCapabilityView.materialize(
                workspace.backend,
                workspace.deployment_volume,
                repertoire,
            )
            catalog = await _ToolCatalog.discover(view)
            deployment = _deployment(workspace)
            result = await deployment.reconcile(_snapshot(), workspace)
            assert result.complete, result.error
            executor = deployment.executor(workspace, revision=_REVISION)
            kernel = EnvironmentKernel(
                workspace.state_root,
                backend=workspace.backend,
                tool_catalog=catalog,
                tool_executor=executor,
            )
            try:
                await _exec(kernel, "export SESSION_KEY=session-value")
                await _exec(kernel, "mkdir -p subdir")
                await _exec(kernel, "cd subdir")

                workspace_value = _text(
                    _output(
                        await _exec(
                            kernel,
                            "tools run \"__import__('os').environ['WS_KEY']\"",
                        )
                    ),
                    "stdout",
                )
                session_value = _text(
                    _output(
                        await _exec(
                            kernel,
                            "tools run \"__import__('os').environ['SESSION_KEY']\"",
                        )
                    ),
                    "stdout",
                )
                virtual_env = _text(
                    _output(
                        await _exec(
                            kernel,
                            "tools run \"__import__('os').environ['VIRTUAL_ENV']\"",
                        )
                    ),
                    "stdout",
                )
                path = _text(
                    _output(
                        await _exec(
                            kernel,
                            "tools run \"__import__('os').environ['PATH']\"",
                        )
                    ),
                    "stdout",
                )
                cwd = _text(
                    _output(
                        await _exec(
                            kernel,
                            "tools run \"__import__('os').getcwd()\"",
                        )
                    ),
                    "stdout",
                )
                host_path = _text(
                    _output(
                        await _exec(
                            kernel,
                            "tools run \"str(Path('{0}').exists())\"".format(
                                "/workspace/../../../host-only"
                            ),
                        )
                    ),
                    "stdout",
                )
                missing = _text(
                    _output(
                        await _exec(
                            kernel,
                            "tools run \"str(Path('{0}').exists())\"".format(
                                "/workspace/host-only"
                            ),
                        )
                    ),
                    "stdout",
                )

                assert workspace_value == "from-workspace\n"
                assert session_value == "session-value\n"
                assert virtual_env.strip().endswith("/.tool-environment/.venv")
                assert virtual_env.strip() + "/bin" in path
                assert cwd == "/workspace/subdir\n"
                assert host_path == "False\n"
                assert missing == "False\n"

                failure = await _exec(kernel, 'tools run "1 / 0"')
                assert _output(failure)["status"] == "failed"
                assert "ZeroDivisionError" in _text(_output(failure), "stderr")
            finally:
                await kernel.close()
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_tool_kill_semantics_leave_no_container(tmp_path: Path) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)
    (repertoire / "tools" / "sleep_tool.py").write_text("VALUE = 1\n", encoding="utf-8")

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            deployment = _deployment(workspace)
            result = await deployment.reconcile(_snapshot(), workspace)
            assert result.complete, result.error
            executor = deployment.executor(workspace, revision=_REVISION)

            queued = executor.prepare(
                _ToolExecutionRequest(
                    code="__import__('time').sleep(30)",
                    bindings=(),
                ),
                _CommandContext(
                    workspace=workspace.root,
                    cwd="/workspace",
                    environment={},
                ),
            )
            await queued.kill()
            status = await queued.run(_NullOutput())
            assert status == ExitStatus(_KILLED_BEFORE_START)
            assert workspace.backend._live_containers == set()

            running = executor.prepare(
                _ToolExecutionRequest(
                    code="__import__('time').sleep(30)",
                    bindings=(),
                ),
                _CommandContext(
                    workspace=workspace.root,
                    cwd="/workspace",
                    environment={},
                ),
            )
            task = asyncio.create_task(running.run(_NullOutput()))
            await asyncio.sleep(0.5)
            assert len(workspace.backend._live_containers) == 1
            await running.kill()
            status = await task
            assert status != 0
            assert workspace.backend._live_containers == set()
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_mcp_discovery_and_invocation_end_to_end(tmp_path: Path) -> None:
    _require_docker()
    project, repertoire = _prepare_project(
        tmp_path,
        env={"MCP_ECHO_TOKEN": "token-value"},
    )
    server_directory = repertoire / "_mcp" / "echo"
    server_directory.mkdir(parents=True, exist_ok=True)
    (server_directory / "server.py").write_text(_MCP_SERVER_SCRIPT, encoding="utf-8")
    (server_directory / "config.json").write_text(
        json.dumps(
            {
                "name": "echo",
                "transport": "stdio",
                "command": [
                    "python3",
                    "/workspace/.workspace/_mcp/echo/server.py",
                ],
                "env": ["MCP_ECHO_TOKEN"],
            }
        ),
        encoding="utf-8",
    )

    async def scenario() -> None:
        workspace = await _open(project, repertoire)
        try:
            view = await _DockerCapabilityView.materialize(
                workspace.backend,
                workspace.deployment_volume,
                repertoire,
            )
            from cli_agent.runtime._capability.provider import CapabilityProvider

            provider = CapabilityProvider(
                view=view,
                workspace=project,
            )
            configs = await provider.discover_mcp_configs()
            assert tuple(config.name for config in configs) == ("echo",)
            deployment = _deployment(workspace)
            facts = await deployment.discover_mcp(configs)
            assert tuple(fact.name for fact in facts) == ("echo",)
            assert tuple(tool.name for tool in facts[0].tools) == ("echo",)

            await deployment.materialize_stubs(workspace, configs, facts)
            stub = await workspace.backend.filesystem.read(
                ".workspace/tools/mcp_echo.py",
            )
            assert b"mcp_binding" in stub

            snapshot = await provider.discover(mcp_configs=configs)
            result = await deployment.reconcile(snapshot, workspace)
            assert result.complete, result.error

            catalog = await _ToolCatalog.discover(view)
            executor = deployment.executor(
                workspace,
                revision=snapshot.revision,
            )
            kernel = EnvironmentKernel(
                workspace.state_root,
                backend=workspace.backend,
                tool_catalog=catalog,
                tool_executor=executor,
            )
            try:
                call = await _exec(
                    kernel,
                    "tools run \"tools.mcp_echo.call('echo', text='hello')\"",
                )
                assert _output(call)["status"] == "exited"
                assert "echo:hello@token-value" in _text(_output(call), "stdout")
            finally:
                await kernel.close()
        finally:
            await workspace.close()
            await _remove_volume(workspace.volume)

    asyncio.run(scenario())


def test_runtime_docker_end_to_end_and_resume(tmp_path: Path, monkeypatch) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)
    (repertoire / "tools" / "math.py").write_text("VALUE = 7\n", encoding="utf-8")

    async def scenario() -> None:
        seed = await _open(project, repertoire)
        await seed.filesystem.write(
            _FileWriteRequest(
                path="AGENTS.md",
                content=b"# Docker workspace rules\nUse tools first.\n",
            )
        )
        await seed.close()

        first_user = UserMessage.text("Run the math tool and a shell command")
        tool_call = ToolCall(
            call_id="docker_tool",
            name="exec",
            arguments={
                "command": 'tools run "tools.math.VALUE"',
                "wait_ms": 60_000,
            },
        )
        shell_call = ToolCall(
            call_id="docker_shell",
            name="exec",
            arguments={"command": "echo from-shell", "wait_ms": 60_000},
        )
        tool_message = AssistantMessage(
            content=(
                TextBlock(text="Running them."),
                tool_call,
                shell_call,
            )
        )
        final_message = AssistantMessage.text("Both finished.")
        provider = ScriptedModelProvider(
            script=(
                (
                    ToolCallReady(call=tool_call),
                    ToolCallReady(call=shell_call),
                    ModelCompletion(
                        message=tool_message,
                        finish_reason="tool_calls",
                    ),
                ),
                (
                    TextDelta(text="Both finished."),
                    ModelCompletion(
                        message=final_message,
                        finish_reason="stop",
                    ),
                ),
                (
                    ModelCompletion(
                        message=AssistantMessage.text("Resumed."),
                        finish_reason="stop",
                    ),
                ),
            )
        )

        runtime = await AgentRuntime.open(
            backend="docker",
            workspace=project,
            repertoire=repertoire,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        session = await runtime.new_session()
        try:
            await _collect_turn(runtime, first_user)
            tool_result = provider.requests[1].messages[3].content[0]
            assert isinstance(tool_result, ToolResult)
            assert _text(_output(tool_result), "stdout") == "7\n"
            assert provider.requests[1].messages[3].content[1] is not None
            assert (
                _stdout(
                    provider.requests[1].messages[3].content[1],
                )
                == "from-shell\n"
            )

            system_message = provider.requests[0].messages[0]
            assert isinstance(system_message, SystemMessage)
            system_body = "\n".join(block.text for block in system_message.content)
            assert "Docker workspace rules" in system_body

            assert runtime._resources.snapshot.revision == (
                runtime._resources.deployment.revision
            )
            session_id = session.session_id
        finally:
            await runtime.close()

        resumed_provider = ScriptedModelProvider(
            script=(
                (
                    ModelCompletion(
                        message=AssistantMessage.text("Resumed."),
                        finish_reason="stop",
                    ),
                ),
            )
        )
        reopened = await AgentRuntime.open(
            backend="docker",
            workspace=project,
            repertoire=repertoire,
            provider=resumed_provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            resumed = await reopened.resume_session(session_id)
            assert resumed.session_id == session_id
            await _collect_turn(
                reopened,
                UserMessage.text("Confirm the tool still works"),
            )
            assert reopened._resources.snapshot.revision == (
                reopened._resources.deployment.revision
            )
            system_message = resumed_provider.requests[0].messages[0]
            assert isinstance(system_message, SystemMessage)
            system_body = "\n".join(block.text for block in system_message.content)
            assert "Docker workspace rules" in system_body
        finally:
            await reopened.close()
            await _remove_volume(reopened._resources.backend.volume)

    asyncio.run(scenario())


def test_delete_session_does_not_remove_the_workspace_volume(
    tmp_path: Path,
) -> None:
    _require_docker()
    project, repertoire = _prepare_project(tmp_path)

    async def scenario() -> None:
        provider = ScriptedModelProvider(
            script=(
                (
                    ModelCompletion(
                        message=AssistantMessage.text("Done."),
                        finish_reason="stop",
                    ),
                ),
            )
        )
        runtime = await AgentRuntime.open(
            backend="docker",
            workspace=project,
            repertoire=repertoire,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        volume = runtime._resources.backend.volume
        session = await runtime.new_session()
        try:
            await runtime.delete_session(session.session_id)
        finally:
            await runtime.close()

        result = subprocess.run(
            ["docker", "volume", "inspect", volume],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, "workspace volume must survive Session delete"

        reopened = await AgentRuntime.open(
            backend="docker",
            workspace=project,
            repertoire=repertoire,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            assert reopened._resources.backend.volume == volume
        finally:
            await reopened.close()
            await _remove_volume(volume)

    asyncio.run(scenario())


def test_unsupported_backend_kind_is_rejected(tmp_path: Path) -> None:
    project, repertoire = _prepare_project(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="unsupported Backend kind"):
            await AgentRuntime.open(
                backend="bogus",
                workspace=project,
                repertoire=repertoire,
                provider=ScriptedModelProvider(script=()),
                user_interaction=_user_interaction,
                context_policy=_context_policy,
            )

    asyncio.run(scenario())


_MCP_SERVER_SCRIPT = r"""
import asyncio
import os

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server(name="echo-server", version="1.0.0")


async def handle_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                description="Echo the given text plus the workspace token",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )


async def handle_call_tool(ctx, params):
    if params.name != "echo":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="unknown tool")],
            is_error=True,
        )
    text = params.arguments.get("text", "")
    token = os.environ.get("MCP_ECHO_TOKEN", "")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"echo:{text}@{token}")]
    )


server.add_request_handler(
    "tools/list", types.PaginatedRequestParams, handle_list_tools
)
server.add_request_handler(
    "tools/call", types.CallToolRequestParams, handle_call_tool
)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
"""


class _NullOutput(ExecutionOutputSink):
    """Discard one execution's output frames."""

    async def write(self, stream: str, data: bytes) -> None:
        del stream, data


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
    *,
    wait_ms: int = 60_000,
) -> ToolResult:
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(command)}",
            name="exec",
            arguments={"command": command, "wait_ms": wait_ms},
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


def _stdout(result: ToolResult) -> str:
    assert isinstance(result.output, dict)
    chunks = result.output["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == "stdout"
    )


async def _collect_turn(
    runtime: AgentRuntime,
    message: UserMessage,
) -> tuple[object, ...]:
    if runtime._binding is None:
        await runtime.new_session()
    return tuple([event async for event in runtime.run_turn(message)])
