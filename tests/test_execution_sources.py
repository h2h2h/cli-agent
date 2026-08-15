"""Shared ExecutionSource suite (RFC-0012 issue 012).

The same scenarios run against Inline, File, Shell, and Tool sources
through one controllable fixture per source: foreground, background,
scheduling, queue overflow, queued and running kill, output truncation,
stdin, and consumer cancellation.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._backend import (
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
)
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.facts import ToolEntry
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.sources import _InlineSource
from cli_agent.runtime._execution import ExecutionOutputSink, ExitStatus


@dataclass
class _CaseRecord:
    """Shared observation seams for one source fixture."""

    release: asyncio.Event = field(default_factory=asyncio.Event)
    started_count: int = 0
    inline_prepares: int = 0
    shell_prepares: int = 0
    tool_prepares: int = 0
    file_writes: int = 0
    kills: int = 0
    shell_inputs: list[bytes] = field(default_factory=list)
    file_contents: list[bytes] = field(default_factory=list)


@dataclass
class _SourceCase:
    name: str
    parallel_safe: bool
    build: Callable[..., EnvironmentKernel]
    record: _CaseRecord
    parallel_command: str
    serial_command: str
    stdin: str
    stdin_rejected: bool
    command_stdin: str | None
    stdout_fragment: str
    stderr_fragment: str | None
    partial_stdout_fragment: str | None
    resource_counter: Callable[[], int]


class _ControlledHandle:
    """One Backend handle that reports output and gates its completion."""

    def __init__(self, record: _CaseRecord) -> None:
        self._record = record

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        self._record.started_count += 1
        await sink.write("stdout", b"first line\n")
        await sink.write("stderr", b"second line\n")
        await self._record.release.wait()
        return ExitStatus(0)

    async def kill(self) -> None:
        self._record.kills += 1
        self._record.release.set()


class _ControlledFilesystem:
    """One filesystem whose writes are recorded and gate on release."""

    def __init__(self, record: _CaseRecord) -> None:
        self._record = record

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        del cwd
        return _ResolvedPath(path=path, within_workspace=True)

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        self._record.started_count += 1
        self._record.file_writes += 1
        self._record.file_contents.append(request.content)
        await self._record.release.wait()
        return _FileWriteResult(
            path=request.path,
            bytes_written=len(request.content),
        )

    async def stat(self, path: str):
        raise AssertionError("controlled filesystem does not stat")

    async def list(self, path: str):
        raise AssertionError("controlled filesystem does not list")

    async def read(self, path: str):
        raise AssertionError("controlled filesystem does not read")

    async def edit(self, request):
        raise AssertionError("controlled filesystem does not edit")

    async def remove(self, path: str, *, recursive: bool = False):
        raise AssertionError("controlled filesystem does not remove")


class _ControlledBackend:
    """One Backend whose shell and tool executions are controlled handles."""

    def __init__(self, record: _CaseRecord, root: str) -> None:
        self.root = root
        self._record = record
        self.filesystem = _ControlledFilesystem(record)

    def prepare_shell(self, request) -> ExecutionOutputSink:
        self._record.shell_prepares += 1
        self._record.shell_inputs.append(request.input_data or b"")
        return _ControlledHandle(self._record)

    def prepare_tool(self, request) -> ExecutionOutputSink:
        del request
        self._record.tool_prepares += 1
        return _ControlledHandle(self._record)


def _inline_handler(record: _CaseRecord):
    async def handler(sink: ExecutionOutputSink) -> ExitStatus:
        record.started_count += 1
        await sink.write("stdout", b"first line\n")
        await sink.write("stderr", b"second line\n")
        await record.release.wait()
        return ExitStatus(0)

    return handler


@pytest.fixture(params=["inline", "files", "shell", "tools"])
def case(request, tmp_path: Path) -> _SourceCase:
    record = _CaseRecord()
    name = request.param

    if name == "inline":

        def build(**overrides):
            def make_fast(r, c):
                del r, c
                record.inline_prepares += 1
                return _InlineExecution(_inline_handler(record))

            def make_slow(r, c):
                del r, c
                record.inline_prepares += 1
                return _InlineExecution(_inline_handler(record))

            return EnvironmentKernel(
                tmp_path,
                custom_sources=(
                    (
                        "fast",
                        _InlineSource(
                            "fast",
                            make_fast,
                            isolated=False,
                            parallel_safe=True,
                        ),
                    ),
                    (
                        "slow",
                        _InlineSource("slow", make_slow, isolated=False),
                    ),
                ),
                **overrides,
            )

        return _SourceCase(
            name=name,
            parallel_safe=True,
            build=build,
            record=record,
            parallel_command="fast",
            serial_command="slow",
            stdin="inline input",
            stdin_rejected=True,
            command_stdin=None,
            stdout_fragment="first line\n",
            stderr_fragment="second line\n",
            partial_stdout_fragment="first line\n",
            resource_counter=lambda: record.inline_prepares,
        )

    if name == "files":
        backend = _ControlledBackend(record, str(tmp_path))

        def build(**overrides):
            return EnvironmentKernel(tmp_path, backend=backend, **overrides)

        return _SourceCase(
            name=name,
            parallel_safe=False,
            build=build,
            record=record,
            parallel_command="files write a.txt",
            serial_command="files write b.txt",
            stdin="hello payload",
            stdin_rejected=False,
            command_stdin="hello payload",
            stdout_fragment="wrote 13 bytes to b.txt",
            stderr_fragment=None,
            partial_stdout_fragment=None,
            resource_counter=lambda: record.file_writes,
        )

    if name == "shell":
        backend = _ControlledBackend(record, str(tmp_path))
        python = Path(sys.executable).name

        def build(**overrides):
            return EnvironmentKernel(
                tmp_path,
                backend=backend,
                parallel_commands=frozenset({python}),
                **overrides,
            )

        return _SourceCase(
            name=name,
            parallel_safe=True,
            build=build,
            record=record,
            parallel_command=f"{python} -c pass",
            serial_command="echo blocked",
            stdin="shell input",
            stdin_rejected=False,
            command_stdin=None,
            stdout_fragment="first line\n",
            stderr_fragment="second line\n",
            partial_stdout_fragment="first line\n",
            resource_counter=lambda: record.shell_prepares,
        )

    backend = _ControlledBackend(record, str(tmp_path))
    catalog = _ToolCatalog(
        (
            ToolEntry(
                name="hello",
                path="tools/hello.py",
                provenance="workspace",
                shadows_repertoire=False,
                valid=True,
                validation_error=None,
                documentation="Say hello.",
                parallel_safe=True,
            ),
            ToolEntry(
                name="slow_tool",
                path="tools/slow_tool.py",
                provenance="workspace",
                shadows_repertoire=False,
                valid=True,
                validation_error=None,
                documentation=None,
                parallel_safe=False,
            ),
        )
    )

    def build(**overrides):
        return EnvironmentKernel(
            tmp_path,
            backend=backend,
            tool_catalog=catalog,
            **overrides,
        )

    return _SourceCase(
        name=name,
        parallel_safe=True,
        build=build,
        record=record,
        parallel_command='tools run "tools.hello.VALUE"',
        serial_command='tools run "tools.slow_tool.VALUE"',
        stdin="tool input",
        stdin_rejected=True,
        command_stdin=None,
        stdout_fragment="first line\n",
        stderr_fragment="second line\n",
        partial_stdout_fragment="first line\n",
        resource_counter=lambda: record.tool_prepares,
    )


def test_foreground(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build()
        try:
            task = asyncio.create_task(
                _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=8_000
                )
            )
            await _wait_started(case.record, 1)
            case.record.release.set()
            snapshot = _output(await task)

            assert snapshot["status"] == "exited"
            assert snapshot["exit_code"] == 0
            assert case.stdout_fragment in _text(snapshot, "stdout")
            if case.stderr_fragment is not None:
                assert case.stderr_fragment in _text(snapshot, "stderr")
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_background(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build()
        try:
            running = _output(
                await _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
                )
            )
            assert running["status"] == "running"
            await _wait_started(case.record, 1)

            partial = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id="output_partial",
                        name="output",
                        arguments={
                            "exec_id": running["exec_id"],
                            "wait_ms": 0,
                        },
                    )
                )
            )
            assert partial["status"] == "running"
            if case.partial_stdout_fragment is not None:
                assert case.partial_stdout_fragment in _text(partial, "stdout")

            case.record.release.set()
            terminal = await _read_until_terminal(kernel, str(running["exec_id"]))

            assert terminal["status"] == "exited"
            assert terminal["exit_code"] == 0
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_scheduling(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build()
        try:
            first = _output(
                await _exec(
                    kernel, case.parallel_command, stdin=case.command_stdin, wait_ms=0
                )
            )
            second = _output(
                await _exec(
                    kernel, case.parallel_command, stdin=case.command_stdin, wait_ms=0
                )
            )

            if case.parallel_safe:
                await _wait_started(case.record, 2)
                assert first["status"] == "running"
                assert second["status"] == "running"
            else:
                await _wait_started(case.record, 1)
                assert first["status"] == "running"
                assert second["status"] == "queued"

            case.record.release.set()
            assert (await _read_until_terminal(kernel, str(first["exec_id"])))[
                "status"
            ] == "exited"
            assert (await _read_until_terminal(kernel, str(second["exec_id"])))[
                "status"
            ] == "exited"
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_queue_overflow(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build(queue_limit=1)
        try:
            running = _output(
                await _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
                )
            )
            queued = _output(
                await _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
                )
            )
            rejected = await _exec(
                kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
            )

            assert running["status"] == "running"
            assert queued["status"] == "queued"
            assert rejected.error is not None
            assert rejected.error["code"] == "queue_full"
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_queued_kill_never_prepares_the_source(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build(queue_limit=1)
        try:
            running = _output(
                await _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
                )
            )
            await _wait_started(case.record, 1)
            queued = _output(
                await _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
                )
            )
            assert queued["status"] == "queued"

            before = case.resource_counter()
            killed = _output(await _kill(kernel, str(queued["exec_id"])))

            assert killed["status"] == "killed"
            assert case.resource_counter() == before
            assert running["status"] == "running"
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_running_kill_terminates_the_handle(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build()
        try:
            running = _output(
                await _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
                )
            )
            await _wait_started(case.record, 1)

            task = asyncio.create_task(_kill(kernel, str(running["exec_id"])))
            await asyncio.sleep(0.05)
            case.record.release.set()
            killed = _output(await task)

            assert killed["status"] == "killed"
            assert killed["is_terminal"] is True
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_output_truncation(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build(chunk_limit=1, byte_limit=10)
        try:
            task = asyncio.create_task(
                _exec(
                    kernel, case.serial_command, stdin=case.command_stdin, wait_ms=8_000
                )
            )
            await _wait_started(case.record, 1)
            case.record.release.set()
            snapshot = _output(await task)

            assert snapshot["truncated"] is True
            assert snapshot["status"] == "exited"
            assert len(snapshot["chunks"]) <= 1
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_stdin(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build()
        try:
            if case.stdin_rejected:
                snapshot = _output(
                    await _exec(
                        kernel,
                        case.serial_command,
                        stdin=case.stdin,
                    )
                )
                assert snapshot["status"] == "failed"
                assert "does not consume exec stdin" in _text(snapshot, "stderr")
            else:
                task = asyncio.create_task(
                    _exec(
                        kernel,
                        case.serial_command,
                        stdin=case.stdin,
                        wait_ms=8_000,
                    )
                )
                await _wait_started(case.record, 1)
                case.record.release.set()
                snapshot = _output(await task)
                assert snapshot["status"] == "exited"
        finally:
            case.record.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_consumer_cancellation(case: _SourceCase) -> None:
    async def scenario() -> None:
        kernel = case.build()
        running = _output(
            await _exec(
                kernel, case.serial_command, stdin=case.command_stdin, wait_ms=0
            )
        )
        await _wait_started(case.record, 1)
        state = kernel._executions[str(running["exec_id"])]

        task = asyncio.create_task(kernel.close())
        await asyncio.sleep(0.05)
        case.record.release.set()
        await task

        assert state.status == "killed"
        assert state.is_terminal is True

    asyncio.run(scenario())


async def _wait_started(record: _CaseRecord, count: int) -> None:
    for _ in range(200):
        if record.started_count >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"execution did not start: {record.started_count}/{count}")


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


async def _kill(kernel: EnvironmentKernel, exec_id: str) -> ToolResult:
    return await kernel.dispatch(
        ToolCall(
            call_id=f"kill_{exec_id}",
            name="kill",
            arguments={"exec_id": exec_id},
        )
    )


async def _output_syscall(
    kernel: EnvironmentKernel,
    exec_id: str,
    *,
    wait_ms: int,
) -> dict[str, object]:
    return _output(
        await kernel.dispatch(
            ToolCall(
                call_id=f"output_{exec_id}",
                name="output",
                arguments={"exec_id": exec_id, "wait_ms": wait_ms},
            )
        )
    )


async def _read_until_terminal(
    kernel: EnvironmentKernel,
    exec_id: str,
) -> dict[str, object]:
    for index in range(200):
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
        await asyncio.sleep(0.01)
    raise AssertionError("execution did not reach a terminal state")


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _text(snapshot: dict[str, object], stream: str) -> str:
    chunks = snapshot["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk["stream"] == stream
    )
