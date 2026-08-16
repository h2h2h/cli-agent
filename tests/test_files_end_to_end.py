"""End-to-end proof of the files commands through the full Runtime path."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from pathlib import Path

from host_fakes import _environment_kernel
from policy_fakes import _AllowAllPolicy
from workspace_fakes import _kernel_workspace

from cli_agent._adapters.local.view import _LocalCapabilityView
from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.sources import _FileSource
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExitStatus,
)


def test_files_write_execution_snapshot_is_fully_observable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            snapshot = _output(
                await _exec(
                    kernel,
                    "files write hello.txt",
                    stdin="line1\nline2\n",
                )
            )

            assert snapshot["status"] == "exited"
            assert snapshot["exit_code"] == 0
            assert snapshot["is_terminal"] is True
            assert {"exec_id", "chunks", "next_cursor", "truncated"} <= snapshot.keys()
            target = tmp_path / "hello.txt"
            assert target.read_text(encoding="utf-8") == "line1\nline2\n"
            assert _stream_text(snapshot, "stdout") == f"wrote 12 bytes to {target}\n"
            assert _stream_text(snapshot, "stderr") == ""
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_files_edit_change_is_visible_in_git_diff(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("line1\n", encoding="utf-8")
    _git_init(tmp_path)

    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            edited = _output(
                await _exec(
                    kernel,
                    "files edit notes.txt",
                    stdin='{"edits": [{"oldText": "line1", "newText": "line2"}]}',
                )
            )
            assert edited["status"] == "exited"
            assert f"replaced 1 block(s) in {target}\n" in _stream_text(
                edited,
                "stdout",
            )

            diff = _output(await _exec(kernel, "git diff"))
            assert "-line1" in _stream_text(diff, "stdout")
            assert "+line2" in _stream_text(diff, "stdout")
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert target.read_text(encoding="utf-8") == "line2\n"


def test_files_write_in_view_under_policy_never_pierces_repertoire(
    tmp_path: Path,
) -> None:
    repertoire = tmp_path / "repertoire"
    (repertoire / "tools").mkdir(parents=True)
    lower = repertoire / "tools" / "calc.py"
    lower.write_text("LOWER = 1\n", encoding="utf-8")
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path, _LocalBackendWorkspace(tmp_path, {})),
            policy=_AllowAllPolicy(),
        )
        try:
            written = _output(
                await _exec(
                    kernel,
                    "files write .workspace/tools/calc.py",
                    stdin="NEW = 2\n",
                )
            )
            assert written["status"] == "exited"
        finally:
            await kernel.close()

    asyncio.run(scenario())

    visible = tmp_path / ".workspace" / "tools" / "calc.py"
    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "NEW = 2\n"
    assert lower.read_text(encoding="utf-8") == "LOWER = 1\n"
    assert asyncio.run(view.inspect("tools/calc.py")).provenance == "workspace"


def test_files_write_cancelled_before_run_creates_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        execution = _FileSource().prepare(
            _ExecutionRequest(
                command=parse_shell_ast("files write partial.txt"),
                stdin="content\n",
            ),
            _CommandContext(
                workspace=str(tmp_path),
                cwd=str(tmp_path),
                environment={},
            ),
        )
        await execution.kill()
        outcome = await execution.run(_DiscardOutput())

        assert outcome == ExitStatus(_KILLED_BEFORE_START)
        assert not (tmp_path / "partial.txt").exists()

    asyncio.run(scenario())


def test_files_edit_rejection_leaves_file_untouched_through_kernel(
    tmp_path: Path,
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("abab\n", encoding="utf-8")

    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            result = _output(
                await _exec(
                    kernel,
                    "files edit notes.txt",
                    stdin='{"edits": [{"oldText": "ab", "newText": "x"}]}',
                )
            )
            assert result["status"] == "failed"
            assert "Found 2 occurrences" in _stream_text(result, "stderr")
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert target.read_text(encoding="utf-8") == "abab\n"


def test_files_write_without_stdin_fails_through_kernel(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            result = _output(await _exec(kernel, "files write missing.txt"))
            assert result["status"] == "failed"
            assert "requires payload in exec.stdin" in _stream_text(result, "stderr")
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert not (tmp_path / "missing.txt").exists()


def test_files_write_content_may_contain_heredoc_markers(tmp_path: Path) -> None:
    content = "hello\nEOF\nworld\nEDI\n"

    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            written = _output(
                await _exec(kernel, "files write data.txt", stdin=content)
            )
            assert written["status"] == "exited"
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert (tmp_path / "data.txt").read_text(encoding="utf-8") == content


def test_files_command_waits_behind_serial_barrier(tmp_path: Path) -> None:
    shell_started = tmp_path / "shell-started"
    shell_release = tmp_path / "shell-release"
    written = tmp_path / "behind.txt"

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            parallel_commands=frozenset({Path(sys.executable).name}),
            parallel_limit=1,
        )
        try:
            shell = _output(
                await _exec(
                    kernel,
                    _blocking_command(shell_started, shell_release),
                    wait_ms=0,
                )
            )
            await _wait_for_path(shell_started)
            queued = _output(
                await _exec(
                    kernel,
                    "files write behind.txt",
                    stdin="x\n",
                    wait_ms=0,
                )
            )

            assert shell["status"] == "running"
            assert queued["status"] == "queued"
            assert not written.exists()

            shell_release.touch(exist_ok=True)
            assert (await _read_until_terminal(kernel, str(queued["exec_id"])))[
                "status"
            ] == "exited"
            assert written.read_text(encoding="utf-8") == "x\n"
        finally:
            shell_release.touch(exist_ok=True)
            await kernel.close()

    asyncio.run(scenario())


def _git_init(workspace: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@cli-agent.local"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "cli-agent test"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=workspace,
        check=True,
    )


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
    *,
    stdin: str | None = None,
    wait_ms: int = 8_000,
) -> ToolResult:
    arguments: dict[str, object] = {"command": command, "wait_ms": wait_ms}
    if stdin is not None:
        arguments["stdin"] = stdin
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(command)}",
            name="exec",
            arguments=arguments,
        )
    )


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _stream_text(snapshot: dict[str, object], stream: str) -> str:
    chunks = snapshot["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk["stream"] == stream
    )


def _blocking_command(started: Path, release: Path) -> str:
    source = (
        "import time; from pathlib import Path; "
        f"started = Path({str(started)!r}); "
        f"release = Path({str(release)!r}); "
        "started.touch(); "
        "\nwhile not release.exists(): time.sleep(0.01)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


async def _read_until_terminal(
    kernel: EnvironmentKernel,
    exec_id: str,
) -> dict[str, object]:
    for index in range(100):
        snapshot = _output(
            await kernel.dispatch(
                ToolCall(
                    call_id=f"output_{index}",
                    name="output",
                    arguments={"exec_id": exec_id, "wait_ms": 100},
                )
            )
        )
        if snapshot["is_terminal"]:
            return snapshot
    raise AssertionError("execution did not reach a terminal state")


async def _wait_for_path(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"path did not appear: {path}")


class _DiscardOutput:
    async def write(self, stream: str, data: bytes) -> None:
        del stream, data
