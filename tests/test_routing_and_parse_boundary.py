"""Contract tests for the Parsed Command validation and routing boundary."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from host_fakes import _environment_kernel
from workspace_fakes import _kernel_workspace

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._backend.local import _LocalBackendWorkspace
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.records import ExecutionRecord
from cli_agent.runtime._environment.router import _ExecutionRoute
from cli_agent.runtime._environment.scheduler import _ExecutionScheduler
from cli_agent.runtime._environment.sources import _InlineSource
from cli_agent.runtime._execution import (
    ExecutionOutputSink,
    ExitStatus,
)

_INVALID_SHELL_COMMAND = {
    "ok": False,
    "code": "invalid_argument",
    "message": "invalid shell command",
}


@pytest.mark.parametrize(
    "command",
    (
        "   ",
        "# only a comment",
        "echo 'unterminated",
        "cat file |",
        "&& echo hi",
        "echo hi >",
        "(echo hi",
        "cmd \\",
        "&",
        "echo `",
        "echo 'x",
    ),
)
def test_parse_failure_returns_invalid_argument_and_creates_no_execution(
    tmp_path: Path,
    command: str,
) -> None:
    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="invalid-shell-command",
                    name="exec",
                    arguments={"command": command},
                )
            )

            assert _error(result) == _INVALID_SHELL_COMMAND
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_parse_failure_in_batch_returns_invalid_argument_and_admits_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path))
        try:
            results = await kernel.dispatch(
                (
                    ToolCall(
                        call_id="valid-call",
                        name="exec",
                        arguments={"command": "true"},
                    ),
                    ToolCall(
                        call_id="malformed-call",
                        name="exec",
                        arguments={"command": "echo 'unterminated"},
                    ),
                )
            )

            assert len(results) == 2
            assert _output(results[0])["status"] == "exited"
            assert _error(results[1]) == _INVALID_SHELL_COMMAND
            assert len(kernel._executions) == 1
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_malformed_custom_command_never_reaches_custom_handler(
    tmp_path: Path,
) -> None:
    prepared: list[str] = []

    def prepare(command, context):
        del command, context
        prepared.append("prepared")

        async def execute(output: ExecutionOutputSink) -> ExitStatus:
            del output
            return ExitStatus(0)

        return _InlineExecution(execute)

    sources = (("cli_read", _InlineSource("cli_read", prepare, isolated=True)),)

    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path), custom_sources=sources)
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="malformed-custom",
                    name="exec",
                    arguments={"command": "cli_read 'unterminated"},
                )
            )

            assert _error(result) == _INVALID_SHELL_COMMAND
            assert prepared == []
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_malformed_tools_heredoc_no_longer_bypasses_parser(
    tmp_path: Path,
) -> None:
    prepared: list[str] = []

    def prepare(command, context):
        del command, context
        prepared.append("prepared")

        async def execute(output: ExecutionOutputSink) -> ExitStatus:
            del output
            return ExitStatus(0)

        return _InlineExecution(execute)

    sources = (("cli_run", _InlineSource("cli_run", prepare, isolated=True)),)

    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path), custom_sources=sources)
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="bypassed-custom-heredoc",
                    name="exec",
                    arguments={"command": "cli_run PY<<\ntools.echo.value()\nPY"},
                )
            )

            assert _error(result) == _INVALID_SHELL_COMMAND
            assert prepared == []
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_parse_failure_never_reaches_capability_view(tmp_path: Path) -> None:
    view = _RaiseOnPrepareView()
    backend = _LocalBackendWorkspace(tmp_path, {})  # type: ignore[arg-type]

    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path, backend))
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="no-capability-prep",
                    name="exec",
                    arguments={"command": "echo hi >"},
                )
            )

            assert _error(result) == _INVALID_SHELL_COMMAND
            assert view.entered is False
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_supervisor_and_scheduler_carry_no_policy_metadata() -> None:
    state_fields = set(ExecutionRecord.__dataclass_fields__)
    assert state_fields.isdisjoint(
        {
            "decision",
            "rule_id",
            "approval_request_id",
            "evaluation",
            "policy",
        }
    )
    assert "request" in state_fields

    route_fields = set(_ExecutionRoute.__dataclass_fields__)
    assert route_fields == {"source", "parallel_safe"}

    assert list(_ExecutionScheduler.admit.__annotations__) == [
        "request",
        "route",
        "return",
    ]


class _RaiseOnPrepareView:
    def __init__(self) -> None:
        self.entered = False

    @asynccontextmanager
    async def prepare_shell(self, command, cwd, *, cancelled):
        del command, cwd, cancelled
        self.entered = True
        yield False


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _error(result: ToolResult) -> dict[str, object]:
    assert result.output is None
    assert isinstance(result.error, dict)
    return result.error
