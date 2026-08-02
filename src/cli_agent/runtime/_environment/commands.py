"""Runtime-trusted command contracts and custom command registry."""

from __future__ import annotations

import os
import shlex
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from pathlib import Path

from cli_agent.runtime._capability.command_parser import CommandParseResult
from cli_agent.runtime._environment.drivers.base import (
    _DriverContext,
    _DriverExecution,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.drivers.executions import _InlineExecution

_CommandContext = _DriverContext
_PreparedExecution = _DriverExecution
_CommandPreparer = Callable[[CommandParseResult, _CommandContext], _PreparedExecution]
_ParallelSafety = bool | Callable[[CommandParseResult], bool]


class _Command(ABC):
    """Describe one Runtime-owned command family."""

    name: str | None
    isolated: bool

    @abstractmethod
    def matches(self, command: CommandParseResult) -> bool:
        """Return whether this command owns the parsed command."""

    @abstractmethod
    def parallel_safe(self, command: CommandParseResult) -> bool:
        """Return whether this command may enter a parallel batch."""

    @abstractmethod
    def prepare(
        self,
        command: CommandParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        """Prepare an execution without starting work or mutating Session state."""


class _CustomCommand(_Command):
    """A Runtime-registered command with a fixed or computed schedule fact."""

    def __init__(
        self,
        *,
        name: str,
        prepare: _CommandPreparer,
        parallel_safe: _ParallelSafety = False,
        isolated: bool = True,
    ) -> None:
        if not name or name.strip() != name or any(
            character.isspace() for character in name
        ):
            raise ValueError("custom command name must be one non-empty token")
        if not isinstance(isolated, bool):
            raise TypeError("custom command isolated must be a bool")
        if not isinstance(parallel_safe, bool) and not callable(parallel_safe):
            raise TypeError("custom command parallel_safe must be a bool or callable")
        self.name = name
        self.isolated = isolated
        self._prepare = prepare
        self._parallel_safe = parallel_safe

    def matches(self, command: CommandParseResult) -> bool:
        """Return whether the first command token matches this command name."""

        return _command_head(command.raw_command) == self.name

    def parallel_safe(self, command: CommandParseResult) -> bool:
        """Evaluate this command's fixed or command-specific schedule fact."""

        value = self._parallel_safe
        return value(command) if callable(value) else value

    def prepare(
        self,
        command: CommandParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        """Construct the command execution without starting it."""

        return self._prepare(command, context)


class _ShellCommand(_Command):
    """The unique Shell fallback command."""

    name = None
    isolated = True

    def __init__(
        self,
        *,
        prepare: _CommandPreparer,
        parallel_commands: frozenset[str] = frozenset(),
    ) -> None:
        invalid = sorted(
            name
            for name in parallel_commands
            if not name or name.strip() != name or "/" in name or "\\" in name
        )
        if invalid:
            raise ValueError(
                "parallel Shell command names must be non-empty executable basenames"
            )
        self._prepare = prepare
        self._parallel_commands = parallel_commands

    def matches(self, command: CommandParseResult) -> bool:
        """Return true because Shell is the fallback for every unmatched command."""

        del command
        return True

    def parallel_safe(self, command: CommandParseResult) -> bool:
        """Return whether the parsed Shell command is trusted for parallel use."""

        return bool(
            command.tokenization_succeeded
            and command.executable_basename in self._parallel_commands
            and not command.contains_shell_composition
        )

    def prepare(
        self,
        command: CommandParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        """Construct the Shell execution without starting it."""

        return self._prepare(command, context)


class _CustomCommandRegistry:
    """Resolve exact Runtime custom command heads before the Shell fallback."""

    def __init__(self, commands: Iterable[_CustomCommand] = ()) -> None:
        self._commands: dict[str, _CustomCommand] = {}
        for command in commands:
            self.register(command)

    def register(self, command: _CustomCommand) -> None:
        """Register one Runtime-owned command without allowing silent replacement."""

        if command.name is None:
            raise ValueError("Shell fallback cannot be registered as a custom command")
        if command.name in self._commands:
            raise ValueError(f"custom command already registered: {command.name}")
        self._commands[command.name] = command

    def resolve(
        self,
        command: CommandParseResult,
    ) -> _CustomCommand | None:
        """Return the custom command selected by the command-head rule."""

        head = _command_head(command.raw_command)
        if head is None:
            return None
        return self._commands.get(head)


def _builtin_custom_commands() -> tuple[_CustomCommand, ...]:
    """Return the built-in Session commands installed in every Kernel."""

    return (
        _CustomCommand(name="cd", prepare=_prepare_cd, isolated=False),
        _CustomCommand(name="export", prepare=_prepare_export, isolated=False),
    )


def _command_head(raw_command: str) -> str | None:
    """Read the first shell token even when a later token is malformed."""

    lexer = shlex.shlex(
        raw_command,
        posix=True,
        punctuation_chars="|&;<>",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return next(lexer)
    except (StopIteration, ValueError):
        return None


def _prepare_cd(
    command: CommandParseResult,
    context: _CommandContext,
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
    context: _CommandContext,
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
