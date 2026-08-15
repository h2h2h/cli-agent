"""Shell Handler request contract and static dependency tests.

RFC-0012 issue 03 requires the Shell Handler to only translate existing
parse facts, Backend cwd, and Session environment into a
``_ShellExecutionRequest``, with no Host subprocess creation, no
``os.environ`` reads, and no concrete Capability View dependency.
"""

import importlib
from pathlib import Path
from typing import Any

from cli_agent.runtime._backend import _ShellExecutionRequest
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.shell import _ShellHandler
from cli_agent.runtime._execution import (
    ExecutionHandle,
    ExecutionOutputSink,
    ExitStatus,
)


class _RecordingBackend:
    """Record every Shell request without starting any work."""

    def __init__(self) -> None:
        self.requests: list[_ShellExecutionRequest] = []

    def prepare_shell(self, request: _ShellExecutionRequest) -> ExecutionHandle:
        self.requests.append(request)
        return _SilentExecution()


class _SilentExecution:
    async def run(self, output: ExecutionOutputSink) -> ExitStatus:
        del output
        return ExitStatus(0)

    async def kill(self) -> None:
        return


def test_shell_handler_emits_backend_neutral_request(tmp_path: Path) -> None:
    backend = _RecordingBackend()
    handler = _ShellHandler(backend)
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={"KEY": "value"},
    )

    handler.prepare(
        _ExecutionRequest(command=parse_shell_ast("echo hi")),
        context,
    )

    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.command is not None
    assert request.cwd == str(tmp_path)
    assert request.environment == {"KEY": "value"}
    assert request.input_data is None
    assert tuple(request.__dataclass_fields__) == (
        "command",
        "cwd",
        "environment",
        "input_data",
    )


def test_shell_handler_encodes_stdin_as_utf8_input_data(tmp_path: Path) -> None:
    backend = _RecordingBackend()
    handler = _ShellHandler(backend)
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )

    handler.prepare(
        _ExecutionRequest(
            command=parse_shell_ast("grep foo"),
            stdin="héllo 世界\n",
        ),
        context,
    )

    assert len(backend.requests) == 1
    assert backend.requests[0].input_data == "héllo 世界\n".encode("utf-8")


def test_shell_handler_keeps_empty_stdin_distinct_from_omitted(
    tmp_path: Path,
) -> None:
    backend = _RecordingBackend()
    handler = _ShellHandler(backend)
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )

    handler.prepare(
        _ExecutionRequest(command=parse_shell_ast("cat"), stdin=""),
        context,
    )

    assert len(backend.requests) == 1
    assert backend.requests[0].input_data == b""


def test_shell_handler_requires_a_backend_workspace(tmp_path: Path) -> None:
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )

    try:
        _ShellHandler().prepare(
            _ExecutionRequest(command=parse_shell_ast("echo hi")),
            context,
        )
    except RuntimeError as exc:
        assert "Backend Workspace" in str(exc)
    else:
        raise AssertionError("Shell handler must fail without a Backend Workspace")


def test_shell_handler_module_has_no_process_or_environment_mechanics() -> None:
    source = _module_source(_ShellHandler.__module__)

    assert "asyncio" not in source
    assert "import os" not in source
    assert "os.environ" not in source
    assert "create_subprocess" not in source
    assert "pathlib" not in source
    assert "capability.view" not in source
    assert "capability_view" not in source


def test_local_backend_is_the_sole_ordinary_shell_subprocess_owner() -> None:
    import cli_agent.runtime._backend.local as local_module

    runtime_source = Path(importlib.import_module("cli_agent.runtime").__file__).parent
    local_root = Path(local_module.__file__).parent
    owners = tuple(
        path
        for path in runtime_source.rglob("*.py")
        if "create_subprocess_shell" in path.read_text(encoding="utf-8")
    )

    assert owners
    assert all(local_root in path.parents for path in owners)


def _module_source(module_name: str) -> str:
    module = importlib.import_module(module_name)
    module_file: Any = module.__file__
    return Path(module_file).read_text(encoding="utf-8")
