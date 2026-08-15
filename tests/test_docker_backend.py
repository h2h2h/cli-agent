"""Docker Backend Workspace tests.

Issue 016 parametrizes the shared Backend / filesystem contract suite over
the Local and Docker implementations and adds Docker-specific resource
audit tests: prepare and queued kill create no container, a running kill
and run cancellation leave nothing behind, parallel executions stay
isolated, close rejects new work, and reopening a volume preserves files.

The Docker cases carry the ``docker`` integration marker and skip when no
daemon is reachable; the CI Docker job must run them against a real
daemon.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from backend_contract_suite import CONTRACT_CASES, _request

from cli_agent.runtime._backend import _BackendWorkspace, _WorkspaceSource
from cli_agent.runtime._backend.docker import (
    _DockerBackend,
    _DockerBackendWorkspace,
    _DockerWorkspaceSource,
)
from cli_agent.runtime._backend.facts import _FileWriteRequest
from cli_agent.runtime._backend.local import _LocalBackend
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionOutputSink,
    ExitStatus,
)
from cli_agent.runtime.model import ToolCall

_DOCKER_IMAGE = "python:3.12-alpine"
_DOCKER_ROOT = "/workspace"

_DOCKER = pytest.param("docker", marks=pytest.mark.docker)


def _docker_available() -> bool:
    """Return whether a reachable Docker daemon exists."""
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


@pytest.fixture(params=["local", _DOCKER])
def open_workspace(
    request: pytest.FixtureRequest,
    tmp_path: Path,
):
    """Yield an async opener for one fresh Backend Workspace per kind."""

    kind = request.param
    opened: list[tuple[str, str | None, object]] = []

    async def opener() -> _BackendWorkspace:
        if kind == "local":
            root = tmp_path / "workspace"
            root.mkdir()
            environment = root / ".workspace" / "env"
            environment.parent.mkdir()
            environment.write_text("", encoding="utf-8")
            workspace = await _LocalBackend().open_workspace(
                _WorkspaceSource(root=root, environment=environment)
            )
            opened.append(("local", None, workspace))
            return workspace
        if not _docker_available():
            pytest.skip("Docker daemon is unavailable")
        volume = f"cli-agent-test-{uuid4().hex}"
        workspace = await _DockerBackend().open_workspace(
            _DockerWorkspaceSource(
                volume=volume,
                image=_DOCKER_IMAGE,
                root=_DOCKER_ROOT,
                environment={},
            )
        )
        opened.append(("docker", volume, workspace))
        return workspace

    yield opener

    async def cleanup() -> None:
        client = None
        for kind, volume, workspace in opened:
            try:
                await workspace.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            if kind != "docker" or volume is None:
                continue
            try:
                from aiodocker import Docker

                if client is None:
                    client = Docker()
                    await client.version()
                volume_object = await client.volumes.get(volume)
                await volume_object.delete(force=True)
            except Exception:
                pass
        if client is not None:
            await client.close()

    asyncio.run(cleanup())


@pytest.fixture
def open_docker_workspace(tmp_path: Path):
    """Yield an async opener for one fresh Docker Backend Workspace."""

    opened: list[tuple[str, object]] = []

    async def opener() -> _DockerBackendWorkspace:
        if not _docker_available():
            pytest.skip("Docker daemon is unavailable")
        volume = f"cli-agent-test-{uuid4().hex}"
        workspace = await _DockerBackend().open_workspace(
            _DockerWorkspaceSource(
                volume=volume,
                image=_DOCKER_IMAGE,
                root=_DOCKER_ROOT,
                environment={},
            )
        )
        opened.append((volume, workspace))
        return workspace

    yield opener

    async def cleanup() -> None:
        client = None
        for volume, workspace in opened:
            try:
                await workspace.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                from aiodocker import Docker

                if client is None:
                    client = Docker()
                    await client.version()
                volume_object = await client.volumes.get(volume)
                await volume_object.delete(force=True)
            except Exception:
                pass
        if client is not None:
            await client.close()

    asyncio.run(cleanup())


@pytest.mark.parametrize(
    "case",
    CONTRACT_CASES,
    ids=[case.__name__.removeprefix("_assert_") for case in CONTRACT_CASES],
)
def test_backend_contract_case(open_workspace, case) -> None:
    async def scenario() -> None:
        await case(open_workspace)

    asyncio.run(scenario())


@pytest.mark.docker
def test_docker_prepare_and_queued_kill_create_no_container(
    open_docker_workspace,
) -> None:
    async def scenario() -> None:
        workspace = await open_docker_workspace()
        assert isinstance(workspace, _DockerBackendWorkspace)
        try:
            execution = workspace.prepare_shell(
                _request("sleep 30", cwd=workspace.root)
            )
            assert workspace._live_containers == set()
            await execution.kill()
            assert workspace._live_containers == set()
            output = await execution.run(_NullOutput())
            assert output == ExitStatus(_KILLED_BEFORE_START)
            assert workspace._live_containers == set()
        finally:
            await workspace.close()

    asyncio.run(scenario())


@pytest.mark.docker
def test_docker_running_kill_leaves_no_container(open_docker_workspace) -> None:
    async def scenario() -> None:
        workspace = await open_docker_workspace()
        try:
            execution = workspace.prepare_shell(
                _request("sleep 30", cwd=workspace.root)
            )
            task = asyncio.create_task(execution.run(_NullOutput()))
            while not workspace._live_containers:
                await asyncio.sleep(0.05)
            assert len(workspace._live_containers) == 1
            await execution.kill()
            outcome = await asyncio.wait_for(task, timeout=10)
            assert outcome > 128
            assert workspace._live_containers == set()
        finally:
            await workspace.close()

    asyncio.run(scenario())


@pytest.mark.docker
def test_docker_run_cancellation_cleans_up(open_docker_workspace) -> None:
    async def scenario() -> None:
        workspace = await open_docker_workspace()
        try:
            execution = workspace.prepare_shell(
                _request("sleep 30", cwd=workspace.root)
            )
            task = asyncio.create_task(execution.run(_NullOutput()))
            while not workspace._live_containers:
                await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert workspace._live_containers == set()
        finally:
            await workspace.close()

    asyncio.run(scenario())


@pytest.mark.docker
def test_docker_parallel_executions_stay_isolated(open_docker_workspace) -> None:
    async def scenario() -> None:
        workspace = await open_docker_workspace()
        try:
            first = workspace.prepare_shell(
                _request(
                    "echo first > first.txt; sleep 1; echo first-done",
                    cwd=workspace.root,
                )
            )
            second = workspace.prepare_shell(
                _request(
                    "echo second > second.txt; sleep 1; echo second-done",
                    cwd=workspace.root,
                )
            )
            first_output = _BufferOutput()
            second_output = _BufferOutput()
            first_task = asyncio.create_task(first.run(first_output))
            second_task = asyncio.create_task(second.run(second_output))
            await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=30)
            assert first_output.text("stdout") == "first-done\n"
            assert second_output.text("stdout") == "second-done\n"
            assert await workspace.filesystem.read("first.txt") == b"first\n"
            assert await workspace.filesystem.read("second.txt") == b"second\n"
        finally:
            await workspace.close()

    asyncio.run(scenario())


@pytest.mark.docker
def test_docker_close_rejects_new_work(open_docker_workspace) -> None:
    async def scenario() -> None:
        workspace = await open_docker_workspace()
        await workspace.close()
        with pytest.raises(RuntimeError, match="Backend Workspace is closed"):
            workspace.prepare_shell(_request("echo nope", cwd=workspace.root))
        with pytest.raises(RuntimeError, match="Backend Workspace is closed"):
            workspace.filesystem.resolve("a.txt", workspace.root)
        with pytest.raises(RuntimeError, match="Backend Workspace is closed"):
            await workspace.filesystem.write(
                _FileWriteRequest(path="a.txt", content=b"x")
            )

    asyncio.run(scenario())


@pytest.mark.docker
def test_docker_volume_reopen_preserves_files(open_docker_workspace) -> None:
    async def scenario() -> None:
        workspace = await open_docker_workspace()
        volume = workspace._source.volume
        await workspace.filesystem.write(
            _FileWriteRequest(path="persisted/notes.txt", content=b"durable")
        )
        await workspace.close()

        reopened = await _DockerBackend().open_workspace(
            _DockerWorkspaceSource(
                volume=volume,
                image=_DOCKER_IMAGE,
                root=_DOCKER_ROOT,
                environment={},
            )
        )
        try:
            assert await reopened.filesystem.read("persisted/notes.txt") == b"durable"
        finally:
            await reopened.close()

    asyncio.run(scenario())


@pytest.mark.docker
def test_docker_kernel_exec_output_and_files_share_the_volume(
    open_docker_workspace,
) -> None:
    async def scenario() -> None:
        workspace = await open_docker_workspace()
        kernel = EnvironmentKernel(workspace.root, backend=workspace)
        try:
            echoed = await _exec(kernel, "echo from-kernel > shared.txt")
            assert echoed.error is None
            written = await _exec(
                kernel,
                "files write shared.txt",
                stdin="from files\n",
            )
            assert written.error is None
            assert await workspace.filesystem.read("shared.txt") == b"from files\n"
            failed = await _exec(kernel, "exit 9")
            assert failed.error is None
        finally:
            await kernel.close()
            await workspace.close()

    asyncio.run(scenario())


class _BufferOutput(ExecutionOutputSink):
    """Collect one execution's stdout frames in memory."""

    def __init__(self) -> None:
        self._text = ""

    async def write(self, stream: str, data: bytes) -> None:
        del stream
        self._text += data.decode("utf-8")

    def text(self, stream: str) -> str:
        del stream
        return self._text


class _NullOutput(ExecutionOutputSink):
    """Discard one execution's output frames."""

    async def write(self, stream: str, data: bytes) -> None:
        del stream, data


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
    *,
    stdin: str | None = None,
) -> object:
    arguments: dict[str, object] = {"command": command, "wait_ms": 8_000}
    if stdin is not None:
        arguments["stdin"] = stdin
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(command)}",
            name="exec",
            arguments=arguments,
        )
    )
