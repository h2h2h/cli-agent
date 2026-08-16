import asyncio
from pathlib import Path
from typing import Literal

from host_fakes import _environment_kernel
from workspace_fakes import _kernel_workspace

from cli_agent.runtime import ToolCall
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalShellExecution,
    _ProcessExecution,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.routing import (
    _ExecutionRoute,
)
from cli_agent.runtime._environment.sources import (
    _builtin_inline_sources,
    _InlineSource,
    _ShellSource,
    _SourceRegistry,
)
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionOutputSink,
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


def test_custom_command_prepares_export_and_shell_handler_prepares_process(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        environment: dict[str, str] = {}
        context = _CommandContext(
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            environment=environment,
        )
        registry = _SourceRegistry(_builtin_inline_sources())

        export = parse_shell_ast("export A=1 MESSAGE='two words'")
        export_spec = registry.resolve(export)
        assert export_spec is not None
        execution = export_spec.prepare(
            _ExecutionRequest(command=export),
            context,
        )

        assert isinstance(execution, _InlineExecution)
        assert environment == {}
        outcome = await execution.run(_BufferOutput())
        assert outcome == ExitStatus(0)
        assert environment == {"A": "1", "MESSAGE": "two words"}

        process_execution = _ShellSource(
            _kernel_workspace(
                tmp_path,
                _LocalBackendWorkspace(tmp_path, {}),
            )
        ).prepare(
            _ExecutionRequest(command=parse_shell_ast("pwd")),
            context,
        )
        assert isinstance(process_execution, _LocalShellExecution)

    asyncio.run(scenario())


def test_inline_export_cancelled_before_run_does_not_mutate_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        environment: dict[str, str] = {}
        registry = _SourceRegistry(_builtin_inline_sources())
        command = parse_shell_ast("export CANCELLED=yes")
        spec = registry.resolve(command)
        assert spec is not None
        execution = spec.prepare(
            _ExecutionRequest(command=command),
            _CommandContext(
                workspace=str(tmp_path),
                cwd=str(tmp_path),
                environment=environment,
            ),
        )

        await execution.kill()
        outcome = await execution.run(_BufferOutput())

        assert outcome == ExitStatus(_KILLED_BEFORE_START)
        assert environment == {}

    asyncio.run(scenario())


def test_process_execution_cancelled_before_run_does_not_spawn() -> None:
    async def scenario() -> None:
        spawned = False

        async def spawn() -> asyncio.subprocess.Process:
            nonlocal spawned
            spawned = True
            raise AssertionError("cancelled execution must not spawn")

        execution = _ProcessExecution(spawn)
        await execution.kill()
        outcome = await execution.run(_BufferOutput())

        assert outcome == ExitStatus(_KILLED_BEFORE_START)
        assert spawned is False

    asyncio.run(scenario())


def test_invalid_inline_export_reports_failure_without_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        environment = {"PRESERVED": "yes"}
        output = _BufferOutput()
        registry = _SourceRegistry(_builtin_inline_sources())
        command = parse_shell_ast("export VALID=value BROKEN")
        spec = registry.resolve(command)
        assert spec is not None
        execution = spec.prepare(
            _ExecutionRequest(command=command),
            _CommandContext(
                workspace=str(tmp_path),
                cwd=str(tmp_path),
                environment=environment,
            ),
        )

        outcome = await execution.run(output)

        assert outcome == ExitStatus(1)
        assert output.chunks == [
            ("stderr", b"Invalid format: BROKEN, expected KEY=VALUE\n")
        ]
        assert environment == {"PRESERVED": "yes"}

    asyncio.run(scenario())


class _FakeExecution:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, output: ExecutionOutputSink) -> ExitStatus:
        self.started.set()
        await output.write("stdout", b"driver output\n")
        await self.cancelled.wait()
        return ExitStatus(_KILLED_BEFORE_START)

    async def kill(self) -> None:
        self.cancelled.set()


class _FakeHandler:
    def __init__(self, execution: _FakeExecution) -> None:
        self.execution = execution
        self.prepared: list[tuple[str, Path, dict[str, str]]] = []

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> _FakeExecution:
        self.prepared.append(
            (request.command.raw_command, context.cwd, dict(context.environment))
        )
        return self.execution


class _FailingHandler:
    def prepare(self, request: _ExecutionRequest, context: _CommandContext):
        del request, context
        raise RuntimeError("preparation failed")


class _SuccessfulExecution:
    async def run(self, output: ExecutionOutputSink) -> ExitStatus:
        del output
        return ExitStatus(0)

    async def kill(self) -> None:
        return


class _SuccessfulHandler:
    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> _SuccessfulExecution:
        del request, context
        return _SuccessfulExecution()


def test_kernel_runs_and_cancels_handle_without_branch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        execution = _FakeExecution()
        handler = _FakeHandler(execution)
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            base_env={"SESSION": "value"},
            queue_limit=1,
            chunk_limit=10,
            byte_limit=1_000,
        )
        state = kernel._manager.admit(
            _ExecutionRequest(command=parse_shell_ast("fake command")),
            _shell_route(handler),
        )
        assert state is not None
        await execution.started.wait()

        await kernel._manager.terminate(state)

        assert handler.prepared == [
            ("fake command", str(tmp_path), {"SESSION": "value"})
        ]
        assert state.status == "killed"
        assert state.exit_code == _KILLED_BEFORE_START
        assert state.handle is execution
        assert state.chunks[0]["text"] == "driver output\n"
        assert state.completion_task is not None
        assert state.completion_task.done()
        await kernel.close()

    asyncio.run(scenario())


def test_parallel_safe_metadata_forces_an_isolated_command_context(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        prepared_context: _CommandContext | None = None

        async def execute(output: ExecutionOutputSink) -> ExitStatus:
            del output
            assert prepared_context is not None
            prepared_context.environment["WORKER"] = "changed"
            return ExitStatus(0)

        def prepare(
            request: _ExecutionRequest, context: _CommandContext
        ) -> _InlineExecution:
            del request
            nonlocal prepared_context
            prepared_context = context
            return _InlineExecution(execute)

        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            base_env={"SESSION": "value"},
            custom_sources=(
                (
                    "worker",
                    _InlineSource(
                        "worker",
                        prepare=prepare,
                        parallel_safe=True,
                        isolated=False,
                    ),
                ),
            ),
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="exec_worker",
                    name="exec",
                    arguments={"command": "worker"},
                )
            )

            assert result.error is None
            assert prepared_context is not None
            assert prepared_context.environment == {
                "SESSION": "value",
                "WORKER": "changed",
            }
            assert prepared_context.set_cwd is None
            assert kernel._env == {"SESSION": "value"}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_handler_preparation_failure_releases_serial_slot_for_queued_execution(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            queue_limit=1,
            chunk_limit=10,
            byte_limit=1_000,
        )
        failed = kernel._manager.admit(
            _ExecutionRequest(command=parse_shell_ast("fake command")),
            _shell_route(_FailingHandler()),
        )
        queued = kernel._manager.admit(
            _ExecutionRequest(command=parse_shell_ast("fake command")),
            _shell_route(_SuccessfulHandler()),
        )
        assert failed is not None
        assert queued is not None
        assert queued.status == "queued"
        assert failed.completion_task is not None

        await failed.completion_task
        assert queued.completion_task is not None
        await queued.completion_task

        assert failed.status == "failed"
        assert queued.status == "exited"
        await kernel.close()

    asyncio.run(scenario())


def _shell_route(handler) -> _ExecutionRoute:
    return _ExecutionRoute(
        source=_InlineSource("fake", handler.prepare, isolated=True),
        parallel_safe=False,
    )
