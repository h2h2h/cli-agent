import asyncio
from pathlib import Path

import pytest

from cli_agent.runtime._capability.command_parser import parse_shell_command
from cli_agent.runtime._environment.commands import (
    _builtin_custom_commands,
    _Command,
    _CustomCommand,
    _CustomCommandRegistry,
    _ShellCommand,
)
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.handlers.shell import _ShellHandler
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import (
    _CommandRouter,
    _ExecutionRoute,
)


class _NullOutput:
    async def write(self, stream: str, data: bytes) -> None:
        del stream, data


def _successful_preparer(command, context):
    del command, context

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        del output
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)


def test_parser_emits_only_generic_shell_syntax_facts() -> None:
    command = parse_shell_command("tools list")

    assert not hasattr(command, "tool")
    assert tuple(command.__dataclass_fields__) == (
        "raw_command",
        "tokens",
        "executable_basename",
        "tokenization_succeeded",
        "contains_shell_composition",
        "contains_output_redirection",
    )


def test_custom_command_contract_and_registry_match_command_heads() -> None:
    command = _CustomCommand(
        name="custom",
        prepare=_successful_preparer,
        parallel_safe=True,
        isolated=False,
    )
    registry = _CustomCommandRegistry((command,))

    assert isinstance(command, _Command)
    assert command.matches(parse_shell_command("custom argument"))
    assert command.matches(parse_shell_command('custom "unterminated'))
    assert registry.resolve(parse_shell_command("custom argument")) is command
    assert registry.resolve(parse_shell_command("./custom argument")) is None
    assert command.parallel_safe(parse_shell_command("custom argument")) is True
    assert command.isolated is False


def test_registry_rejects_duplicate_custom_command_names() -> None:
    registry = _CustomCommandRegistry()
    registry.register(
        _CustomCommand(name="duplicate", prepare=_successful_preparer)
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            _CustomCommand(name="duplicate", prepare=_successful_preparer)
        )


def test_router_returns_command_and_parallel_safe_without_driver_fields() -> None:
    registry = _CustomCommandRegistry(_builtin_custom_commands())
    router = _CommandRouter(
        shell_command=_ShellCommand(
            prepare=_ShellHandler().prepare,
            parallel_commands=frozenset({"cat"}),
        ),
        custom_registry=registry,
    )

    custom_route = router.route(
        ExecutionDecision.allow(parse_shell_command("export A=1"))
    )
    shell_route = router.route(
        ExecutionDecision.allow(parse_shell_command("cat file.txt"))
    )

    assert isinstance(custom_route, _ExecutionRoute)
    assert custom_route.command.name == "export"
    assert custom_route.parallel_safe is False
    assert shell_route.command.name is None
    assert shell_route.parallel_safe is True
    assert tuple(field.name for field in _ExecutionRoute.__dataclass_fields__.values()) == (
        "command",
        "parallel_safe",
    )


def test_prepare_does_not_mutate_session_before_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        environment: dict[str, str] = {}
        context = _CommandContext(
            workspace=tmp_path,
            cwd=tmp_path,
            environment=environment,
        )
        command = parse_shell_command("export KEY=value")
        custom = _builtin_custom_commands()[1]

        execution = custom.prepare(command, context)

        assert environment == {}
        assert await execution.run(_NullOutput()) == _ExecutionOutcome.exited()
        assert environment == {"KEY": "value"}

    asyncio.run(scenario())
