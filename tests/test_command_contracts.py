import asyncio
from pathlib import Path

import pytest

from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.routing import (
    _CommandRouter,
    _ExecutionRoute,
)
from cli_agent.runtime._environment.sources import (
    ExecutionSource,
    _builtin_inline_sources,
    _InlineSource,
    _ShellSource,
    _SourceRegistry,
)
from cli_agent.runtime._execution import (
    ExecutionOutputSink,
    ExitStatus,
)


class _NullOutput:
    async def write(self, stream: str, data: bytes) -> None:
        del stream, data


def _successful_preparer(request, context):
    del request, context

    async def execute(output: ExecutionOutputSink) -> ExitStatus:
        del output
        return ExitStatus(0)

    return _InlineExecution(execute)


def test_parser_emits_only_generic_shell_syntax_facts() -> None:
    command = parse_shell_ast("tools list")

    assert not hasattr(command, "tool")
    assert tuple(command.__dataclass_fields__) == (
        "raw_command",
        "root",
        "syntax_valid",
        "contains_shell_composition",
        "contains_output_redirection",
    )


def test_inline_source_contract_and_registry_match_command_heads() -> None:
    source = _InlineSource(
        "custom",
        prepare=_successful_preparer,
        parallel_safe=True,
        isolated=False,
    )
    registry = _SourceRegistry((("custom", source),))

    assert isinstance(source, ExecutionSource)
    assert registry.resolve(parse_shell_ast("custom argument")) is source
    assert registry.resolve(parse_shell_ast('custom "unterminated')) is None
    assert registry.resolve(parse_shell_ast("./custom argument")) is None
    assert source.parallel_safe(parse_shell_ast("custom argument")) is True
    assert source.isolated is False


def test_registry_rejects_duplicate_source_names() -> None:
    registry = _SourceRegistry(
        (
            (
                "duplicate",
                _InlineSource("duplicate", _successful_preparer, isolated=True),
            ),
        )
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            "duplicate",
            _InlineSource("duplicate", _successful_preparer, isolated=True),
        )


def test_router_returns_source_and_parallel_safe_without_driver_fields() -> None:
    registry = _SourceRegistry(_builtin_inline_sources())
    router = _CommandRouter(
        shell_source=_ShellSource(parallel_commands=frozenset({"cat"})),
        sources=registry,
    )

    custom_route = router.resolve(parse_shell_ast("export A=1"))
    shell_route = router.resolve(parse_shell_ast("cat file.txt"))

    assert isinstance(custom_route, _ExecutionRoute)
    assert isinstance(custom_route.source, _InlineSource)
    assert custom_route.source.name == "export"
    assert custom_route.parallel_safe is False
    assert isinstance(shell_route.source, _ShellSource)
    assert shell_route.parallel_safe is True
    assert tuple(
        field.name for field in _ExecutionRoute.__dataclass_fields__.values()
    ) == (
        "source",
        "parallel_safe",
    )


def test_router_resolve_has_no_policy_or_scheduler_dependencies() -> None:
    registry = _SourceRegistry(_builtin_inline_sources())
    router = _CommandRouter(
        shell_source=_ShellSource(parallel_commands=frozenset({"cat"})),
        sources=registry,
    )

    assert set(vars(router)) == {"_shell_source", "_sources"}
    assert tuple(_ExecutionRoute.__dataclass_fields__) == (
        "source",
        "parallel_safe",
    )
    route = router.resolve(parse_shell_ast("cat file.txt"))
    assert isinstance(route.source, _ShellSource)
    assert route.parallel_safe is True
    assert router.resolve(parse_shell_ast("cat file.txt")) == route


def test_prepare_does_not_mutate_session_before_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        environment: dict[str, str] = {}
        context = _CommandContext(
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            environment=environment,
        )
        command = parse_shell_ast("export KEY=value")
        export = dict(_builtin_inline_sources())["export"]

        execution = export.prepare(
            _ExecutionRequest(command=command),
            context,
        )

        assert environment == {}
        assert await execution.run(_NullOutput()) == ExitStatus(0)
        assert environment == {"KEY": "value"}

    asyncio.run(scenario())
