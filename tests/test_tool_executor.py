"""Issue 15: ToolExecutor converts deployed Tools into ExecutionHandles.

These tests pin the RFC-0015 ToolExecutor contract: ``prepare`` is
synchronous and side-effect free, stale or foreign or incomplete
deployments and invalid bindings are rejected before any side effect with
classified failures (never command exit codes), a queued kill never starts
the worker, worker failures surface as exit codes, and infrastructure
failures raise ``BackendExecutionError``.
"""

import asyncio
from pathlib import Path

import pytest

from cli_agent._adapters.local.executor import _LocalToolExecutor
from cli_agent._adapters.local.view import _LocalCapabilityView
from cli_agent.runtime._backend import _ToolBinding, _ToolExecutionRequest
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _ProcessExecution,
)
from cli_agent.runtime._capability.deployment import (
    DeploymentSnapshot,
    ToolExecutor,
    ToolRuntimeSnapshot,
)
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._environment.handlers.base import _CommandContext
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    BackendExecutionError,
    ExecutionOutputSink,
    ExitStatus,
)

_WORKSPACE_ID = "local:00000000000000000000000000000000"
_REVISION = "active-revision"


def _complete_deployment(
    *,
    revision: str = _REVISION,
    workspace_id: str = _WORKSPACE_ID,
) -> DeploymentSnapshot:
    return DeploymentSnapshot(
        workspace_id=workspace_id,
        revision=revision,
        layout_version=1,
        complete=True,
        error=None,
        tool_runtime=ToolRuntimeSnapshot(
            python="/nonexistent/python",
            worker="/nonexistent/worker.py",
            tools_directory="/nonexistent/tools",
            binding_directory="/nonexistent/runtime",
            error=None,
        ),
    )


def _context(root: Path, *, cwd: str = ".") -> _CommandContext:
    return _CommandContext(
        workspace=str(root),
        cwd=cwd,
        environment={"SESSION_KEY": "session-value"},
    )


def _executor(
    backend: _LocalBackendWorkspace,
    *,
    deployment: DeploymentSnapshot,
    revision: str = _REVISION,
    workspace_id: str = _WORKSPACE_ID,
) -> _LocalToolExecutor:
    return _LocalToolExecutor(
        backend,
        workspace_id=workspace_id,
        revision=revision,
        deployment=deployment,
        runtime=deployment.tool_runtime,
    )


def _backend(tmp_path: Path) -> _LocalBackendWorkspace:
    _prepare_workspace(tmp_path)
    repertoire = tmp_path / "repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    return _LocalBackendWorkspace(tmp_path, {})


def test_executor_conforms_to_the_tool_executor_protocol(
    tmp_path: Path,
) -> None:
    executor = _executor(_backend(tmp_path), deployment=_complete_deployment())

    assert isinstance(executor, ToolExecutor)


def test_prepare_is_synchronous_and_creates_no_process(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    executor = _executor(backend, deployment=_complete_deployment())

    execution = executor.prepare(
        _ToolExecutionRequest(code="VALUE", bindings=()),
        _context(tmp_path),
    )

    assert isinstance(execution, _ProcessExecution)
    assert execution._process is None


def test_stale_deployment_is_rejected_before_any_side_effect(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    executor = _executor(
        backend,
        deployment=_complete_deployment(revision="deployed-revision"),
    )

    async def scenario() -> None:
        execution = executor.prepare(
            _ToolExecutionRequest(code="VALUE", bindings=()),
            _context(tmp_path),
        )

        assert isinstance(execution, _InlineExecution)
        assert not isinstance(execution, _ProcessExecution)

        chunks: list[bytes] = []

        class Sink:
            async def write(self, stream: str, data: bytes) -> None:
                del stream
                chunks.append(data)

        assert await execution.run(Sink()) == ExitStatus(1)
        assert b"stale" in b"".join(chunks)

    asyncio.run(scenario())


def test_foreign_deployment_is_rejected_before_any_side_effect(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    executor = _executor(
        backend,
        deployment=_complete_deployment(
            workspace_id="local:11111111111111111111111111111111",
        ),
    )

    async def scenario() -> None:
        execution = executor.prepare(
            _ToolExecutionRequest(code="VALUE", bindings=()),
            _context(tmp_path),
        )

        assert isinstance(execution, _InlineExecution)
        assert not isinstance(execution, _ProcessExecution)

        chunks: list[bytes] = []

        class Sink:
            async def write(self, stream: str, data: bytes) -> None:
                del stream
                chunks.append(data)

        assert await execution.run(Sink()) == ExitStatus(1)
        assert b"different Workspace" in b"".join(chunks)

    asyncio.run(scenario())


def test_incomplete_deployment_reports_the_deployment_error(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    executor = _executor(
        backend,
        deployment=DeploymentSnapshot(
            workspace_id=_WORKSPACE_ID,
            revision=_REVISION,
            layout_version=1,
            complete=False,
            error="Tool environment is unavailable: sync failed",
        ),
    )

    async def scenario() -> None:
        execution = executor.prepare(
            _ToolExecutionRequest(code="VALUE", bindings=()),
            _context(tmp_path),
        )

        assert isinstance(execution, _InlineExecution)
        assert not isinstance(execution, _ProcessExecution)

        chunks: list[bytes] = []

        class Sink:
            async def write(self, stream: str, data: bytes) -> None:
                del stream
                chunks.append(data)

        assert await execution.run(Sink()) == ExitStatus(1)
        assert b"sync failed" in b"".join(chunks)

    asyncio.run(scenario())


def test_invalid_bindings_are_rejected_before_any_side_effect(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    executor = _executor(backend, deployment=_complete_deployment())

    async def scenario() -> None:
        for bad in (
            _ToolBinding(name="not valid", path="tools/x.py"),
            _ToolBinding(name="class", path="tools/x.py"),
            _ToolBinding(name="escape", path="../outside.py"),
            _ToolBinding(name="escape", path="/absolute.py"),
            _ToolBinding(name="escape", path="skills/other.py"),
        ):
            execution = executor.prepare(
                _ToolExecutionRequest(code="VALUE", bindings=(bad,)),
                _context(tmp_path),
            )

            assert isinstance(execution, _InlineExecution), bad
            assert not isinstance(execution, _ProcessExecution), bad

        valid = executor.prepare(
            _ToolExecutionRequest(
                code="VALUE",
                bindings=(_ToolBinding(name="ok", path="tools/ok.py"),),
            ),
            _context(tmp_path),
        )
        assert isinstance(valid, _ProcessExecution)

    asyncio.run(scenario())


def test_worker_run_streams_output_and_maps_failures_to_exit_codes(
    tmp_path: Path,
) -> None:
    from cli_agent._adapters.local.deployment import _LocalCapabilityDeployment
    from cli_agent._workspaces import _LocalWorkspace
    from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
    from cli_agent.runtime._capability.snapshot import (
        CAPABILITY_SCHEMA_VERSION,
        CapabilitySnapshot,
    )
    from cli_agent.runtime._capability.tools.catalog import _ToolCatalog

    _prepare_workspace(tmp_path)
    repertoire = tmp_path.parent / f"{tmp_path.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(tmp_path, {})
    deployment = _LocalCapabilityDeployment()
    opened = _LocalWorkspace(_WORKSPACE_ID, tmp_path, backend, repertoire)
    snapshot = CapabilitySnapshot(
        revision=_REVISION,
        schema_version=CAPABILITY_SCHEMA_VERSION,
        tools=_ToolCatalog(()),
        skills=_SkillCatalog(()),
        mcp_servers=(),
        project_instructions=None,
    )
    deployed = asyncio.run(deployment.reconcile(snapshot, opened))
    assert deployed.complete, deployed.error
    executor = _executor(backend, deployment=deployed)

    async def scenario() -> None:
        chunks: dict[str, bytes] = {"stdout": b"", "stderr": b""}

        class Sink:
            async def write(self, stream: str, data: bytes) -> None:
                chunks[stream] += data

        success = executor.prepare(
            _ToolExecutionRequest(
                code="print('hello'); print('world', file=__import__('sys').stderr)",
                bindings=(),
            ),
            _context(tmp_path),
        )
        assert await success.run(Sink()) == ExitStatus(0)
        assert chunks["stdout"] == b"hello\n"
        assert chunks["stderr"] == b"world\n"

        failure = executor.prepare(
            _ToolExecutionRequest(
                code="raise ValueError('boom')",
                bindings=(),
            ),
            _context(tmp_path),
        )
        chunks["stdout"] = b""
        chunks["stderr"] = b""
        assert await failure.run(Sink()) == ExitStatus(1)
        assert b"ValueError: boom" in chunks["stderr"]

    asyncio.run(scenario())


def test_missing_worker_environment_is_a_mechanism_failure(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    executor = _executor(backend, deployment=_complete_deployment())

    async def scenario() -> None:
        execution = executor.prepare(
            _ToolExecutionRequest(code="VALUE", bindings=()),
            _context(tmp_path),
        )

        assert isinstance(execution, _ProcessExecution)
        with pytest.raises(BackendExecutionError):
            await execution.run(_NullOutput())

    asyncio.run(scenario())


def test_queued_kill_never_starts_the_worker(tmp_path: Path) -> None:
    from cli_agent._adapters.local.deployment import _LocalCapabilityDeployment
    from cli_agent._workspaces import _LocalWorkspace
    from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
    from cli_agent.runtime._capability.snapshot import (
        CAPABILITY_SCHEMA_VERSION,
        CapabilitySnapshot,
    )
    from cli_agent.runtime._capability.tools.catalog import _ToolCatalog

    _prepare_workspace(tmp_path)
    repertoire = tmp_path.parent / f"{tmp_path.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(tmp_path, {})
    deployment = _LocalCapabilityDeployment()
    opened = _LocalWorkspace(_WORKSPACE_ID, tmp_path, backend, repertoire)
    snapshot = CapabilitySnapshot(
        revision=_REVISION,
        schema_version=CAPABILITY_SCHEMA_VERSION,
        tools=_ToolCatalog(()),
        skills=_SkillCatalog(()),
        mcp_servers=(),
        project_instructions=None,
    )
    deployed = asyncio.run(deployment.reconcile(snapshot, opened))
    assert deployed.complete, deployed.error
    executor = _executor(backend, deployment=deployed)

    async def scenario() -> None:
        execution = executor.prepare(
            _ToolExecutionRequest(
                code="from pathlib import Path\nPath('marker.txt').write_text('ran')\n",
                bindings=(),
            ),
            _context(tmp_path),
        )

        assert isinstance(execution, _ProcessExecution)
        await execution.kill()
        assert await execution.run(_NullOutput()) == ExitStatus(_KILLED_BEFORE_START)
        assert not (tmp_path / "marker.txt").exists()

    asyncio.run(scenario())


def test_prepare_fails_closed_after_the_backend_closes(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    executor = _executor(backend, deployment=_complete_deployment())

    async def scenario() -> None:
        await backend.close()
        with pytest.raises(RuntimeError, match="closed"):
            executor.prepare(
                _ToolExecutionRequest(code="VALUE", bindings=()),
                _context(tmp_path),
            )

    asyncio.run(scenario())


class _NullOutput(ExecutionOutputSink):
    async def write(self, stream: str, data: bytes) -> None:
        del stream, data
        return None
