import asyncio
import shlex
import sys
from pathlib import Path

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime.capability.command_parser import parse_shell_command


def test_parser_reports_only_generic_shell_syntax_facts() -> None:
    direct = parse_shell_command("  export A=1 MESSAGE='two words' EMPTY=  ")
    malformed = parse_shell_command("export VALID=value BROKEN")

    assert direct.raw_command == "  export A=1 MESSAGE='two words' EMPTY=  "
    assert direct.tokens == (
        "export",
        "A=1",
        "MESSAGE=two words",
        "EMPTY=",
    )
    assert direct.executable_basename == "export"
    assert direct.tokenization_succeeded is True
    assert direct.contains_shell_composition is False
    assert malformed.tokens == ("export", "VALID=value", "BROKEN")
    assert malformed.executable_basename == "export"
    assert malformed.tokenization_succeeded is True
    assert malformed.contains_shell_composition is False

    for command in (
        "sh -c 'export A=1'",
        "export A=1 | cat",
        "export A=1 && true",
        "export A=1\ntrue",
        "export A=1 > result",
        "export A=$(printf child)",
        "export A=`printf child`",
        "A=1 true",
    ):
        parsed = parse_shell_command(command)
        assert parsed.raw_command == command


def test_top_level_export_is_atomic_and_session_private(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel_a = EnvironmentKernel(
            tmp_path,
            base_env={"BASE": "workspace"},
        )
        kernel_b = EnvironmentKernel(
            tmp_path,
            base_env={"BASE": "workspace"},
        )
        try:
            exported = _output(
                await _exec(
                    kernel_a,
                    "export BASE=overridden EMPTY= MESSAGE='two words'",
                )
            )
            assert exported["status"] == "exited"
            assert exported["exit_code"] == 0
            assert kernel_a._env == {
                "BASE": "overridden",
                "EMPTY": "",
                "MESSAGE": "two words",
            }
            assert kernel_b._env == {"BASE": "workspace"}

            before_invalid = dict(kernel_a._env)
            invalid = _output(await _exec(kernel_a, "export NEW=value BROKEN"))
            assert invalid["status"] == "failed"
            assert invalid["exit_code"] == 1
            assert "expected KEY=VALUE" in _stream_text(invalid, "stderr")
            assert kernel_a._env == before_invalid
        finally:
            await kernel_a.close()
            await kernel_b.close()

    asyncio.run(scenario())


def test_export_uses_shell_lane_fifo_and_queued_kill_prevents_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = tmp_path / "started"
        release = tmp_path / "release"
        kernel = EnvironmentKernel(tmp_path, queue_limit=2)
        try:
            running = _output(
                await _exec(
                    kernel,
                    _blocking_command(started, release),
                    wait_ms=0,
                )
            )
            await _wait_for_path(started)
            cancelled = _output(await _exec(kernel, "export CANCELLED=yes", wait_ms=0))
            retained = _output(await _exec(kernel, "export RETAINED=yes", wait_ms=0))
            assert running["status"] == "running"
            assert cancelled["status"] == "queued"
            assert retained["status"] == "queued"
            assert kernel._env == {}

            killed = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id="kill_export",
                        name="kill",
                        arguments={"exec_id": cancelled["exec_id"]},
                    )
                )
            )
            assert killed["status"] == "killed"

            release.touch()
            terminal = await _read_until_terminal(
                kernel,
                str(retained["exec_id"]),
            )
            assert terminal["status"] == "exited"
            assert kernel._env == {"RETAINED": "yes"}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_nested_export_uses_shell_while_custom_export_rejects_composition(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(
            tmp_path,
            base_env={"BASE": "workspace"},
        )
        try:
            nested = _output(await _exec(kernel, "sh -c 'export CHILD=wrapper'"))
            assert nested["status"] == "exited"
            assert kernel._env == {"BASE": "workspace"}

            for command in (
                "export CHILD=pipeline | cat",
                "export CHILD=compound && true",
                "export CHILD=$(printf substitution)",
            ):
                snapshot = _output(await _exec(kernel, command))
                assert snapshot["status"] == "failed", command
                assert kernel._env == {"BASE": "workspace"}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_session_close_kills_queued_export_without_mutation(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = tmp_path / "close-started"
        release = tmp_path / "close-release"
        kernel = EnvironmentKernel(tmp_path)
        await _exec(
            kernel,
            _blocking_command(started, release),
            wait_ms=0,
        )
        await _wait_for_path(started)
        queued = _output(await _exec(kernel, "export MUST_NOT_EXIST=yes", wait_ms=0))
        queued_state = kernel._executions[str(queued["exec_id"])]

        await kernel.close()

        assert queued_state.status == "killed"
        assert queued_state.completion_task is None
        assert kernel._env == {}
        await kernel.close()

    asyncio.run(scenario())


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
