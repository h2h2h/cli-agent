import asyncio
import shlex
import sys
from pathlib import Path

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.commands import (
    _builtin_custom_commands,
    _CustomCommand,
    _CustomCommandRegistry,
    _ShellCommand,
)
from cli_agent.runtime._environment.handlers.base import (
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.handlers.shell import _ShellHandler
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import (
    _CommandRouter,
)


def test_router_prefers_custom_registry_and_keeps_process_choice_private() -> None:
    def prepare_cli_read(command, context):
        del command, context

        async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
            del output
            return _ExecutionOutcome.exited()

        return _InlineExecution(execute)

    registry = _CustomCommandRegistry(
        (
            *_builtin_custom_commands(),
            _CustomCommand(
                name="cli_read",
                prepare=prepare_cli_read,
                parallel_safe=True,
            ),
        )
    )
    router = _CommandRouter(
        shell_command=_ShellCommand(
            prepare=_ShellHandler().prepare,
            parallel_commands=frozenset({"cat"}),
        ),
        custom_registry=registry,
    )

    export_route = router.route(ExecutionDecision.allow(parse_shell_ast("export A=1")))
    read_route = router.route(
        ExecutionDecision.allow(parse_shell_ast("cli_read file.txt"))
    )
    cat_route = router.route(ExecutionDecision.allow(parse_shell_ast("cat file.txt")))
    pipeline_route = router.route(
        ExecutionDecision.allow(parse_shell_ast("cat file.txt | head"))
    )

    assert isinstance(export_route.command, _CustomCommand)
    assert export_route.command.name == "export"
    assert export_route.command.isolated is False
    assert export_route.parallel_safe is False
    assert isinstance(read_route.command, _CustomCommand)
    assert read_route.command.name == "cli_read"
    assert read_route.command.isolated is True
    assert read_route.parallel_safe is True
    assert isinstance(cat_route.command, _ShellCommand)
    assert cat_route.command.isolated is True
    assert cat_route.parallel_safe is True
    assert isinstance(pipeline_route.command, _ShellCommand)
    assert pipeline_route.parallel_safe is False


def test_cd_is_a_session_persistent_custom_command(tmp_path: Path) -> None:
    async def scenario() -> None:
        child = tmp_path / "child"
        child.mkdir()
        kernel = EnvironmentKernel(tmp_path)
        try:
            changed = _output(await _exec(kernel, "cd child"))
            pwd = _output(await _exec(kernel, "pwd"))
            reset = _output(await _exec(kernel, "cd"))
            root_pwd = _output(await _exec(kernel, "pwd"))

            assert changed["status"] == "exited"
            assert _stream_text(changed, "stdout") == str(child)
            assert _stream_text(pwd, "stdout").strip() == str(child)
            assert reset["status"] == "exited"
            assert _stream_text(root_pwd, "stdout").strip() == str(tmp_path)
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_parallel_shell_batch_respects_serial_custom_barrier(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_started = tmp_path / "first-started"
        first_release = tmp_path / "first-release"
        second_started = tmp_path / "second-started"
        second_release = tmp_path / "second-release"
        observed = tmp_path / "observed"
        kernel = EnvironmentKernel(
            tmp_path,
            queue_limit=4,
            parallel_limit=2,
            parallel_commands=frozenset({Path(sys.executable).name}),
        )
        try:
            first = _output(
                await _exec(
                    kernel,
                    _blocking_command(first_started, first_release),
                    wait_ms=0,
                )
            )
            second = _output(
                await _exec(
                    kernel,
                    _blocking_command(second_started, second_release),
                    wait_ms=0,
                )
            )
            await asyncio.gather(
                _wait_for_path(first_started),
                _wait_for_path(second_started),
            )

            barrier = _output(await _exec(kernel, "export BARRIER=passed", wait_ms=0))
            observer = _output(
                await _exec(
                    kernel,
                    _python_command(
                        "import os; from pathlib import Path; "
                        f"Path({str(observed)!r}).write_text("
                        "os.environ.get('BARRIER', 'missing'))"
                    ),
                    wait_ms=0,
                )
            )

            assert first["status"] == "running"
            assert second["status"] == "running"
            assert barrier["status"] == "queued"
            assert observer["status"] == "queued"
            assert not observed.exists()

            first_release.touch()
            second_release.touch()
            assert (await _read_until_terminal(kernel, str(barrier["exec_id"])))[
                "status"
            ] == "exited"
            assert (await _read_until_terminal(kernel, str(observer["exec_id"])))[
                "status"
            ] == "exited"
            assert observed.read_text() == "passed"
        finally:
            first_release.touch(exist_ok=True)
            second_release.touch(exist_ok=True)
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
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"path did not appear: {path}")


def _blocking_command(started: Path, release: Path) -> str:
    return _python_command(
        "import time; from pathlib import Path; "
        f"started = Path({str(started)!r}); "
        f"release = Path({str(release)!r}); "
        "started.touch(); "
        "\nwhile not release.exists(): time.sleep(0.01)"
    )


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
        if isinstance(chunk, dict) and chunk["stream"] == stream
    )
