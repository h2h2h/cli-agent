import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._environment import EnvironmentKernel


def test_shell_child_receives_host_environment_with_session_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("M5_HOST_ONLY", "host-first")
    monkeypatch.setenv("M5_COLLISION", "host-value")
    monkeypatch.setenv("CLI_AGENT_API_KEY", "provider-secret")

    async def scenario() -> None:
        kernel_a = EnvironmentKernel(
            tmp_path,
            base_env={
                "M5_COLLISION": "workspace-value",
                "M5_WORKSPACE_ONLY": "workspace-secret",
            },
        )
        kernel_b = EnvironmentKernel(
            tmp_path,
            base_env={
                "M5_COLLISION": "workspace-value",
                "M5_WORKSPACE_ONLY": "workspace-secret",
            },
        )
        try:
            first = await _read_environment(kernel_a)
            assert first == {
                "CLI_AGENT_API_KEY": "provider-secret",
                "M5_COLLISION": "workspace-value",
                "M5_HOST_ONLY": "host-first",
                "M5_WORKSPACE_ONLY": "workspace-secret",
            }
            assert os.environ["M5_COLLISION"] == "host-value"

            exported = _output(
                await _exec(
                    kernel_a,
                    "export M5_COLLISION=session-value M5_SESSION_ONLY=present",
                )
            )
            assert exported["status"] == "exited"

            monkeypatch.setenv("M5_HOST_ONLY", "host-later")
            second = await _read_environment(kernel_a)
            other_session = await _read_environment(kernel_b)
            assert second == {
                "CLI_AGENT_API_KEY": "provider-secret",
                "M5_COLLISION": "session-value",
                "M5_HOST_ONLY": "host-later",
                "M5_SESSION_ONLY": "present",
                "M5_WORKSPACE_ONLY": "workspace-secret",
            }
            assert other_session == {
                "CLI_AGENT_API_KEY": "provider-secret",
                "M5_COLLISION": "workspace-value",
                "M5_HOST_ONLY": "host-later",
                "M5_WORKSPACE_ONLY": "workspace-secret",
            }
            assert os.environ["M5_COLLISION"] == "host-value"
            assert "M5_SESSION_ONLY" not in os.environ

            inherited_values = (
                "provider-secret",
                "workspace-secret",
                "host-first",
                "host-later",
            )
            for state in kernel_a._executions.values():
                command = state.request.command
                rendered = repr(command)
                assert all(value not in rendered for value in inherited_values)
        finally:
            await kernel_a.close()
            await kernel_b.close()

    asyncio.run(scenario())


def test_child_environment_is_bound_when_queued_execution_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("M5_START_TIME", "before")

    async def scenario() -> None:
        started = tmp_path / "started"
        release = tmp_path / "release"
        kernel = EnvironmentKernel(tmp_path)
        try:
            await _exec(
                kernel,
                _blocking_command(started, release),
                wait_ms=0,
            )
            await _wait_for_path(started)
            queued = _output(
                await _exec(
                    kernel,
                    _python_command("import os; print(os.environ['M5_START_TIME'])"),
                    wait_ms=0,
                )
            )
            assert queued["status"] == "queued"

            monkeypatch.setenv("M5_START_TIME", "after")
            release.touch()
            terminal = await _read_until_terminal(
                kernel,
                str(queued["exec_id"]),
            )

            assert terminal["status"] == "exited"
            assert _stream_text(terminal, "stdout") == "after\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


async def _read_environment(kernel: EnvironmentKernel) -> dict[str, str]:
    names = (
        "CLI_AGENT_API_KEY",
        "M5_COLLISION",
        "M5_HOST_ONLY",
        "M5_SESSION_ONLY",
        "M5_WORKSPACE_ONLY",
    )
    source = (
        "import json, os; "
        f"names = {names!r}; "
        "print(json.dumps({name: os.environ[name] for name in names if name in os.environ}, sort_keys=True))"
    )
    snapshot = _output(await _exec(kernel, _python_command(source)))
    assert snapshot["status"] == "exited"
    chunks = snapshot["chunks"]
    assert isinstance(chunks, list)
    text = "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == "stdout"
    )
    loaded = json.loads(text)
    assert isinstance(loaded, dict)
    return {str(key): str(value) for key, value in loaded.items()}


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
    *,
    wait_ms: int = 8_000,
) -> ToolResult:
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(command)}",
            name="exec",
            arguments={"command": command, "wait_ms": wait_ms},
        )
    )


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
                    arguments={
                        "exec_id": exec_id,
                        "wait_ms": 100,
                    },
                )
            )
        )
        if snapshot["is_terminal"]:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("execution did not reach a terminal state")


async def _wait_for_path(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"path did not appear: {path}")


def _blocking_command(started: Path, release: Path) -> str:
    source = (
        "import pathlib, time; "
        f"pathlib.Path({str(started)!r}).touch(); "
        f"release = pathlib.Path({str(release)!r}); "
        "exec('while not release.exists():\\n    time.sleep(0.01)')"
    )
    return _python_command(source)


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


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
        if isinstance(chunk, dict) and chunk.get("stream") == stream
    )
