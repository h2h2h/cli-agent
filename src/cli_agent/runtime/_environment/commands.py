"""AEP-style Runtime-trusted custom command registry and built-in handlers."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from cli_agent.runtime._environment.drivers.base import (
    _DriverContext,
    _DriverExecution,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.drivers.executions import _InlineExecution
from cli_agent.runtime.capability.command_parser import CommandParseResult

_CustomCommandPreparer = Callable[
    [CommandParseResult, _DriverContext],
    _DriverExecution,
]


@dataclass(frozen=True, slots=True)
class _CustomCommandSpec:
    """Runtime-trusted implementation and scheduling facts for one command."""

    name: str
    prepare: _CustomCommandPreparer
    parallel_safe: bool = False

    def __post_init__(self) -> None:
        normalized = self.name.strip()
        if not normalized or normalized != self.name or any(
            character.isspace() for character in normalized
        ):
            raise ValueError("custom command name must be one non-empty token")


class _CustomCommandRegistry:
    """Resolve exact command heads before the Shell fallback."""

    def __init__(self, commands: Iterable[_CustomCommandSpec] = ()) -> None:
        self._commands: dict[str, _CustomCommandSpec] = {}
        for command in commands:
            self.register(command)

    def register(self, command: _CustomCommandSpec) -> None:
        """Register or deliberately override one Runtime-owned command."""

        self._commands[command.name] = command

    def resolve(
        self,
        command: CommandParseResult,
    ) -> _CustomCommandSpec | None:
        """Return the handler selected by the AEP command-head rule."""

        if command.tool is not None:
            return None
        if not command.tokenization_succeeded or not command.tokens:
            return None
        return self._commands.get(command.tokens[0])


def _builtin_custom_commands() -> tuple[_CustomCommandSpec, ...]:
    """Return the built-ins installed in every Environment Kernel."""

    return (
        _CustomCommandSpec(name="cd", prepare=_prepare_cd),
        _CustomCommandSpec(name="export", prepare=_prepare_export),
    )


def _prepare_cd(
    command: CommandParseResult,
    context: _DriverContext,
) -> _InlineExecution:
    args = command.tokens[1:]

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        target = context.workspace if not args else _target_path(args[0], context.cwd)
        new_cwd = Path(os.path.normpath(str(target)))
        if not new_cwd.exists():
            await output.write(
                "stderr",
                f"Directory not found: {new_cwd}\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        if not new_cwd.is_dir():
            await output.write(
                "stderr",
                f"Not a directory: {new_cwd}\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        if context.set_cwd is None:
            return _ExecutionOutcome.failed(1)

        context.set_cwd(new_cwd)
        text = str(new_cwd)
        if not _is_within_workspace(new_cwd, context.workspace):
            text = (
                f"{text}\n"
                "Warning: You are outside the workspace. "
                "Run `cd` to return to the workspace root."
            )
        await output.write("stdout", text.encode())
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)


def _prepare_export(
    command: CommandParseResult,
    context: _DriverContext,
) -> _InlineExecution:
    args = command.tokens[1:]

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        if not args:
            text = "\n".join(
                f"{key}={value}" for key, value in context.environment.items()
            )
            await output.write(
                "stdout",
                (text or "(no custom env vars)").encode(),
            )
            return _ExecutionOutcome.exited()

        assignments: list[tuple[str, str]] = []
        for item in args:
            if "=" not in item:
                await output.write(
                    "stderr",
                    f"Invalid format: {item}, expected KEY=VALUE\n".encode(),
                )
                return _ExecutionOutcome.failed(1)
            assignments.append(tuple(item.split("=", 1)))

        context.environment.update(assignments)
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)


def _target_path(target: str, cwd: Path) -> Path:
    path = Path(target)
    if path.is_absolute() or target.startswith(("/", "\\")):
        return path
    if len(target) > 1 and target[1] == ":":
        return path
    return cwd / path


def _is_within_workspace(path: Path, workspace: Path) -> bool:
    path_norm = os.path.normcase(os.path.normpath(str(path)))
    workspace_norm = os.path.normcase(os.path.normpath(str(workspace)))
    try:
        common = os.path.commonpath([path_norm, workspace_norm])
    except ValueError:
        return False
    return common == workspace_norm
