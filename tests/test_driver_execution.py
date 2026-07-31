import asyncio
from pathlib import Path
from typing import Literal

from cli_agent.runtime._capability.command_parser import parse_shell_command
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.commands import (
    _builtin_custom_commands,
    _CustomCommandRegistry,
)
from cli_agent.runtime._environment.drivers.base import (
    _DriverContext,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.drivers.executions import (
    _InlineExecution,
    _ProcessExecution,
)
from cli_agent.runtime._environment.drivers.shell import _ShellDriver
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import (
    _DriverKind,
    _ExecutionRoute,
    _SchedulingClass,
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


def test_custom_driver_prepares_export_and_shell_driver_prepares_process(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        environment: dict[str, str] = {}
        context = _DriverContext(
            workspace=tmp_path,
            cwd=tmp_path,
            environment=environment,
        )
        registry = _CustomCommandRegistry(_builtin_custom_commands())

        export = parse_shell_command("export A=1 MESSAGE='two words'")
        export_spec = registry.resolve(export)
        assert export_spec is not None
        execution = export_spec.prepare(export, context)

        assert isinstance(execution, _InlineExecution)
        assert environment == {}
        outcome = await execution.run(_BufferOutput())
        assert outcome == _ExecutionOutcome.exited()
        assert environment == {"A": "1", "MESSAGE": "two words"}

        process_execution = _ShellDriver().prepare(
            parse_shell_command("pwd"),
            context,
        )
        assert isinstance(process_execution, _ProcessExecution)

    asyncio.run(scenario())


def test_inline_export_cancelled_before_run_does_not_mutate_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        environment: dict[str, str] = {}
        registry = _CustomCommandRegistry(_builtin_custom_commands())
        command = parse_shell_command("export CANCELLED=yes")
        spec = registry.resolve(command)
        assert spec is not None
        execution = spec.prepare(
            command,
            _DriverContext(
                workspace=tmp_path,
                cwd=tmp_path,
                environment=environment,
            ),
        )

        await execution.cancel()
        outcome = await execution.run(_BufferOutput())

        assert outcome == _ExecutionOutcome.killed()
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
        await execution.cancel()
        outcome = await execution.run(_BufferOutput())

        assert outcome == _ExecutionOutcome.killed()
        assert spawned is False

    asyncio.run(scenario())


def test_invalid_inline_export_reports_failure_without_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        environment = {"PRESERVED": "yes"}
        output = _BufferOutput()
        registry = _CustomCommandRegistry(_builtin_custom_commands())
        command = parse_shell_command("export VALID=value BROKEN")
        spec = registry.resolve(command)
        assert spec is not None
        execution = spec.prepare(
            command,
            _DriverContext(
                workspace=tmp_path,
                cwd=tmp_path,
                environment=environment,
            ),
        )

        outcome = await execution.run(output)

        assert outcome == _ExecutionOutcome.failed(1)
        assert output.chunks == [
            ("stderr", b"Invalid format: BROKEN, expected KEY=VALUE\n")
        ]
        assert environment == {"PRESERVED": "yes"}

    asyncio.run(scenario())


class _FakeExecution:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        self.started.set()
        await output.write("stdout", b"driver output\n")
        await self.cancelled.wait()
        return _ExecutionOutcome.killed()

    async def cancel(self) -> None:
        self.cancelled.set()


class _FakeDriver:
    def __init__(self, execution: _FakeExecution) -> None:
        self.execution = execution
        self.prepared: list[tuple[str, Path, dict[str, str]]] = []

    def prepare(
        self,
        command,
        context: _DriverContext,
    ) -> _FakeExecution:
        self.prepared.append(
            (command.raw_command, context.cwd, dict(context.environment))
        )
        return self.execution


class _FailingDriver:
    def prepare(self, command, context: _DriverContext):
        del command, context
        raise RuntimeError("preparation failed")


class _SuccessfulExecution:
    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        del output
        return _ExecutionOutcome.exited()

    async def cancel(self) -> None:
        return


class _SuccessfulDriver:
    def prepare(self, command, context: _DriverContext) -> _SuccessfulExecution:
        del command, context
        return _SuccessfulExecution()


def test_kernel_runs_and_cancels_driver_execution_without_driver_branch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        execution = _FakeExecution()
        driver = _FakeDriver(execution)
        kernel = EnvironmentKernel(
            tmp_path,
            base_env={"SESSION": "value"},
            queue_limit=1,
            chunk_limit=10,
            byte_limit=1_000,
        )
        decision = ExecutionDecision.allow(parse_shell_command("fake command"))
        state = kernel._supervisor.admit(
            decision,
            _shell_route(driver),
        )
        assert state is not None
        await execution.started.wait()

        await kernel._supervisor.terminate(state)

        assert driver.prepared == [("fake command", tmp_path, {"SESSION": "value"})]
        assert state.status == "killed"
        assert state.exit_code is None
        assert state.driver_execution is execution
        assert state.chunks[0]["text"] == "driver output\n"
        assert state.completion_task is not None
        assert state.completion_task.done()
        await kernel.close()

    asyncio.run(scenario())


def test_driver_preparation_failure_releases_lane_for_queued_execution(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(
            tmp_path,
            queue_limit=1,
            chunk_limit=10,
            byte_limit=1_000,
        )
        decision = ExecutionDecision.allow(parse_shell_command("fake command"))
        failed = kernel._supervisor.admit(
            decision,
            _shell_route(_FailingDriver()),
        )
        queued = kernel._supervisor.admit(
            decision,
            _shell_route(_SuccessfulDriver()),
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


def _shell_route(driver) -> _ExecutionRoute:
    return _ExecutionRoute(
        driver_kind=_DriverKind.SHELL,
        scheduling=_SchedulingClass.SERIAL,
        driver=driver,
    )
