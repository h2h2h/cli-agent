import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalCapabilityView,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.routing import (
    _CommandRouter,
)
from cli_agent.runtime._environment.sources import (
    _builtin_inline_sources,
    _FileSource,
    _InlineSource,
    _ShellSource,
    _SourceRegistry,
)
from cli_agent.runtime._execution import (
    ExecutionOutputSink,
    ExitStatus,
)


def test_router_prefers_custom_registry_and_keeps_process_choice_private() -> None:
    def prepare_cli_read(command, context):
        del command, context

        async def execute(output: ExecutionOutputSink) -> ExitStatus:
            del output
            return ExitStatus(0)

        return _InlineExecution(execute)

    registry = _SourceRegistry(
        (
            *_builtin_inline_sources(),
            (
                "cli_read",
                _InlineSource(
                    "cli_read",
                    prepare_cli_read,
                    parallel_safe=True,
                    isolated=True,
                ),
            ),
        )
    )
    router = _CommandRouter(
        shell_source=_ShellSource(parallel_commands=frozenset({"cat"})),
        sources=registry,
    )

    export_route = router.resolve(parse_shell_ast("export A=1"))
    read_route = router.resolve(parse_shell_ast("cli_read file.txt"))
    cat_route = router.resolve(parse_shell_ast("cat file.txt"))
    pipeline_route = router.resolve(parse_shell_ast("cat file.txt | head"))

    assert isinstance(export_route.source, _InlineSource)
    assert export_route.source.name == "export"
    assert export_route.source.isolated is False
    assert export_route.parallel_safe is False
    assert isinstance(read_route.source, _InlineSource)
    assert read_route.source.name == "cli_read"
    assert read_route.source.isolated is True
    assert read_route.parallel_safe is True
    assert isinstance(cat_route.source, _ShellSource)
    assert cat_route.source.isolated is True
    assert cat_route.parallel_safe is True
    assert isinstance(pipeline_route.source, _ShellSource)
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


def test_files_command_resolves_to_custom_route_and_is_serial(
    tmp_path: Path,
) -> None:
    repertoire = tmp_path / "repertoire"
    (repertoire / "tools").mkdir(parents=True)
    lower = repertoire / "tools" / "calc.py"
    lower.write_text("LOWER = 1\n", encoding="utf-8")
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)

    async def scenario() -> None:
        kernel = EnvironmentKernel(
            tmp_path, backend=_LocalBackendWorkspace(tmp_path, {}, view)
        )
        try:
            registered = kernel._router._sources.resolve(
                parse_shell_ast("files write f <<'EOF'\nx\nEOF")
            )
            assert registered is not None
            assert isinstance(registered, _FileSource)
            assert registered.name == "files"
            assert registered.isolated is True

            written = _output(
                await _exec(
                    kernel,
                    "files write .workspace/tools/calc.py",
                    stdin="NEW = 2\n",
                )
            )
            assert written["status"] == "exited"
            assert "wrote" in _stream_text(written, "stdout")
            state = kernel._executions[str(written["exec_id"])]
            assert state.route.source.name == "files"
            assert state.route.parallel_safe is False

            invalid = _output(await _exec(kernel, "files nonsense hello"))
            assert invalid["status"] == "failed"
            assert "unknown files subcommand" in _stream_text(invalid, "stderr")
            state = kernel._executions[str(invalid["exec_id"])]
            assert state.route.source.name == "files"
        finally:
            await kernel.close()

    asyncio.run(scenario())

    visible = tmp_path / ".workspace" / "tools" / "calc.py"
    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "NEW = 2\n"
    assert lower.read_text(encoding="utf-8") == "LOWER = 1\n"


def test_files_command_cannot_be_silently_overridden(tmp_path: Path) -> None:
    def prepare_duplicate(command, context):
        del command, context

        async def execute(output: ExecutionOutputSink) -> ExitStatus:
            del output
            return ExitStatus(0)

        return _InlineExecution(execute)

    with pytest.raises(ValueError, match="already registered"):
        EnvironmentKernel(
            tmp_path,
            custom_sources=(
                (
                    "files",
                    _InlineSource(
                        "files",
                        prepare_duplicate,
                        isolated=True,
                    ),
                ),
            ),
        )


@pytest.mark.parametrize(
    "raw",
    (
        "./files write f <<'EOF'\nx\nEOF",
        "/bin/files write f <<'EOF'\nx\nEOF",
        "/usr/local/bin/files write f <<'EOF'\nx\nEOF",
        "env files write f <<'EOF'\nx\nEOF",
    ),
)
def test_files_head_is_not_matched_by_path_qualified_commands(
    tmp_path: Path,
    raw: str,
) -> None:
    kernel = EnvironmentKernel(tmp_path)

    resolved = kernel._router._sources.resolve(parse_shell_ast(raw))

    assert resolved is None
    assert kernel._router.resolve(parse_shell_ast(raw)).source.name is None


@pytest.mark.parametrize(
    "raw",
    (
        "files nonsense f",
        "files write f",
        "files write f | cat",
        "files write f <<'EOF' > out.txt\nx\nEOF",
        "files write \"$VAR\" <<'EOF'\nx\nEOF",
        "files edit f <<'EDI'\nnot json\nEDI",
    ),
)
def test_malformed_files_forms_fail_on_files_route_not_shell(
    tmp_path: Path,
    raw: str,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            snapshot = _output(await _exec(kernel, raw))
            assert snapshot["status"] == "failed"
            state = kernel._executions[str(snapshot["exec_id"])]
            assert state.route.source.name == "files"
            assert state.route.parallel_safe is False
        finally:
            await kernel.close()

    asyncio.run(scenario())


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
