"""Local Backend Shell execution tests.

These tests pin RFC-0012 issue 03: the Local Backend is the sole owner of
ordinary Shell subprocesses, the process is created only when ``run()``
starts, the execution base environment is merged with the Session overlay,
and Capability redirect copy-up still happens before the process spawns.
"""

import asyncio
from pathlib import Path
from typing import Literal

import pytest

from cli_agent._adapters.local.overlay import _LocalCapabilityOverlay
from cli_agent._adapters.local.view import _LocalCapabilityView
from cli_agent.runtime._backend import _ShellExecutionRequest
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalShellExecution,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExitStatus,
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
    input_data: bytes | None = None,
) -> _ShellExecutionRequest:
    return _ShellExecutionRequest(
        command=parse_shell_ast(command),
        cwd=str(cwd),
        environment=environment or {},
        input_data=input_data,
    )


def test_prepare_is_synchronous_and_run_spawns_the_shell(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})

        execution = backend.prepare_shell(_request("echo hi", cwd=tmp_path))
        assert isinstance(execution, _LocalShellExecution)
        assert not asyncio.iscoroutine(execution)

        output = _BufferOutput()
        assert await execution.run(output) == ExitStatus(0)
        assert output.text("stdout") == "hi\n"

    asyncio.run(scenario())


def test_failed_command_reports_exit_code(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(_request("exit 3", cwd=tmp_path)).run(
            output
        )

        assert outcome == ExitStatus(3)

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

        assert outcome == ExitStatus(0)
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

        assert outcome == ExitStatus(0)
        assert output.text("stdout").strip() == str(subdirectory.resolve())

    asyncio.run(scenario())


def test_queued_before_run_cancel_does_not_spawn(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        execution = backend.prepare_shell(_request("echo hi", cwd=tmp_path))

        await execution.kill()
        output = _BufferOutput()
        outcome = await execution.run(output)

        assert outcome == ExitStatus(_KILLED_BEFORE_START)
        assert output.chunks == []

    asyncio.run(scenario())


def test_cancel_terminates_a_running_shell(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        execution = backend.prepare_shell(_request("sleep 30", cwd=tmp_path))
        output = _BufferOutput()

        task = asyncio.create_task(execution.run(output))
        await asyncio.sleep(0.2)
        await execution.kill()
        outcome = await task

        assert outcome == ExitStatus(143)

    asyncio.run(scenario())


def test_capability_redirect_copy_up_happens_before_spawn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    lower = repertoire / "tools" / "message.txt"
    lower.parent.mkdir(parents=True)
    lower.write_text("lower\n", encoding="utf-8")
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    visible = workspace / ".workspace" / "tools" / "message.txt"

    async def scenario() -> None:
        backend = _LocalBackendWorkspace(workspace, {})
        output = _BufferOutput()
        request = _request(
            "echo changed > .workspace/tools/message.txt",
            cwd=workspace,
        )
        execution = _LocalCapabilityOverlay(view).wrap_shell(
            request.command,
            request.cwd,
            backend.prepare_shell(request),
        )

        outcome = await execution.run(output)

        assert outcome == ExitStatus(0)
        assert lower.read_text(encoding="utf-8") == "lower\n"
        assert not visible.is_symlink()
        assert visible.read_text(encoding="utf-8") == "changed\n"

    asyncio.run(scenario())


def test_shell_without_input_data_inherits_host_stdin(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(
            _request("python3 -c 'import sys; print(sys.stdin.isatty())'", cwd=tmp_path)
        ).run(output)

        assert outcome == ExitStatus(0)
        assert output.text("stdout") == f"{__import__('sys').stdin.isatty()}\n"

    asyncio.run(scenario())


def test_shell_with_empty_input_data_sends_immediate_eof(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(
            _request(
                "python3 -c 'import sys; print(sys.stdin.isatty()); print(repr(sys.stdin.read()))'",
                cwd=tmp_path,
                input_data=b"",
            )
        ).run(output)

        assert outcome == ExitStatus(0)
        assert output.text("stdout") == "False\n''\n"

    asyncio.run(scenario())


def test_shell_receives_utf8_input_data(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(
            _request(
                "cat",
                cwd=tmp_path,
                input_data="héllo 世界\n".encode("utf-8"),
            )
        ).run(output)

        assert outcome == ExitStatus(0)
        assert output.text("stdout") == "héllo 世界\n"

    asyncio.run(scenario())


def test_shell_sequence_consumes_input_data_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(
            _request("cat; wc -c | tr -d ' '", cwd=tmp_path, input_data=b"hello")
        ).run(output)

        assert outcome == ExitStatus(0)
        assert output.text("stdout") == "hello0\n"

    asyncio.run(scenario())


def test_large_bidirectional_shell_io_does_not_deadlock(tmp_path: Path) -> None:
    payload = b"x" * (1024 * 1024)

    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(
            _request(
                "python3 -c 'import sys; "
                "sys.stdout.buffer.write(b\"y\" * 700_000); "
                "sys.stdout.buffer.flush(); "
                "data = sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(data)'",
                cwd=tmp_path,
                input_data=payload,
            )
        ).run(output)

        assert outcome == ExitStatus(0)
        stdout = b"".join(data for name, data in output.chunks if name == "stdout")
        assert stdout == (b"y" * 700_000) + payload
        assert len([1 for name, _ in output.chunks if name == "stdout"]) > 1

    asyncio.run(scenario())


def test_process_closing_stdin_early_does_not_hang(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        output = _BufferOutput()

        outcome = await backend.prepare_shell(
            _request(
                "head -c 4",
                cwd=tmp_path,
                input_data=b"a" * (1024 * 1024),
            )
        ).run(output)

        assert outcome == ExitStatus(0)
        assert output.text("stdout") == "aaaa"

    asyncio.run(scenario())


def test_cancel_with_pending_input_leaves_no_subprocess(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        execution = backend.prepare_shell(
            _request(
                "python3 -c 'import time; time.sleep(30)'",
                cwd=tmp_path,
                input_data=b"x" * (1024 * 1024),
            )
        )
        output = _BufferOutput()

        task = asyncio.create_task(execution.run(output))
        await asyncio.sleep(0.2)
        await execution.kill()
        outcome = await asyncio.wait_for(task, timeout=2)

        assert outcome == ExitStatus(143)
        assert b"".join(data for _, data in output.chunks) == b""

    asyncio.run(scenario())


def test_cancelled_run_task_kills_process_and_reaps_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _LocalBackendWorkspace(tmp_path, {})
        execution = backend.prepare_shell(
            _request(
                "python3 -c 'import time; time.sleep(30)'",
                cwd=tmp_path,
                input_data=b"x" * (1024 * 1024),
            )
        )
        output = _BufferOutput()

        task = asyncio.create_task(execution.run(output))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)

        assert execution._process._process is not None
        assert execution._process._process.returncode is not None

    asyncio.run(scenario())
