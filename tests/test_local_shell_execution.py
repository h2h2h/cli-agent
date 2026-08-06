"""Local Backend Shell execution tests.

These tests pin RFC-0012 issue 03: the Local Backend is the sole owner of
ordinary Shell subprocesses, the process is created only when ``run()``
starts, the execution base environment is merged with the Session overlay,
and Capability redirect copy-up still happens before the process spawns.
"""

import asyncio
from pathlib import Path
from typing import Literal

from cli_agent.runtime._backend import _ShellExecutionRequest
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalShellExecution,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._environment.handlers.base import (
    _ExecutionOutcome,
)


class _BufferOutput:
    def __init__(self) -> None:
        self.chunks: list[tuple[str, bytes]] = []

    async def write(
        self,
        stream: Literal["stdout", "stderr"],
        data: bytes,
    ) -> None:
        self.chunks.append((stream, data))

    def text(self, stream: str) -> str:
        return "".join(
            data.decode("utf-8") for name, data in self.chunks if name == stream
        )


def _request(
    command: str,
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> _ShellExecutionRequest:
    return _ShellExecutionRequest(
        command=parse_shell_ast(command),
        cwd=str(cwd),
        environment=environment or {},
    )


def test_prepare_is_synchronous_and_run_spawns_the_shell(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})

        execution = backend.prepare_shell(_request("echo hi", cwd=tmp_path))
        assert isinstance(execution, _LocalShellExecution)
        assert not asyncio.iscoroutine(execution)

        output = _BufferOutput()
        assert await execution.run(output) == _ExecutionOutcome.exited()
        assert output.text("stdout") == "hi\n"

    asyncio.run(scenario())


def test_failed_command_reports_exit_code(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(_request("exit 3", cwd=tmp_path)).run(
            output
        )

        assert outcome == _ExecutionOutcome.failed(3)

    asyncio.run(scenario())


def test_session_environment_overlays_workspace_and_host_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MERGE_KEY", "from-host")
    monkeypatch.setenv("HOST_ONLY", "host")

    async def scenario() -> None:
        backend = _LocalBackendWorkspace(
            tmp_path,
            {"MERGE_KEY": "from-workspace", "WORKSPACE_ONLY": "workspace"},
        )
        output = _BufferOutput()
        command = 'printf "%s|%s|%s" "$MERGE_KEY" "$WORKSPACE_ONLY" "$HOST_ONLY"'

        outcome = await backend.prepare_shell(
            _request(
                command,
                cwd=tmp_path,
                environment={"MERGE_KEY": "from-session"},
            )
        ).run(output)

        assert outcome == _ExecutionOutcome.exited()
        assert output.text("stdout") == "from-session|workspace|host"

    asyncio.run(scenario())


def test_shell_runs_in_the_request_cwd(tmp_path: Path) -> None:
    subdirectory = tmp_path / "sub"
    subdirectory.mkdir()

    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(_request("pwd", cwd=subdirectory)).run(
            output
        )

        assert outcome == _ExecutionOutcome.exited()
        assert output.text("stdout").strip() == str(subdirectory.resolve())

    asyncio.run(scenario())


def test_queued_before_run_cancel_does_not_spawn(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        execution = backend.prepare_shell(_request("echo hi", cwd=tmp_path))

        await execution.cancel()
        output = _BufferOutput()
        outcome = await execution.run(output)

        assert outcome == _ExecutionOutcome.killed()
        assert output.chunks == []

    asyncio.run(scenario())


def test_cancel_terminates_a_running_shell(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        execution = backend.prepare_shell(_request("sleep 30", cwd=tmp_path))
        output = _BufferOutput()

        task = asyncio.create_task(execution.run(output))
        await asyncio.sleep(0.2)
        await execution.cancel()
        outcome = await task

        assert outcome.status == "killed"

    asyncio.run(scenario())


def test_capability_redirect_copy_up_happens_before_spawn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    lower = repertoire / "tools" / "message.txt"
    lower.parent.mkdir(parents=True)
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "message.txt"

    async def scenario() -> None:
        backend = _LocalBackendWorkspace(workspace, {})
        backend.bind_capability_view(view)
        output = _BufferOutput()

        outcome = await backend.prepare_shell(
            _request("echo changed > .workspace/tools/message.txt", cwd=workspace)
        ).run(output)

        assert outcome == _ExecutionOutcome.exited()
        assert lower.read_text(encoding="utf-8") == "lower\n"
        assert not visible.is_symlink()
        assert visible.read_text(encoding="utf-8") == "changed\n"

    asyncio.run(scenario())
