"""Runtime-trusted command contracts and custom command registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable

from cli_agent.runtime._backend import _WorkspaceFilesystem
from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.cd import _prepare_cd
from cli_agent.runtime._environment.handlers.executions import _text_execution
from cli_agent.runtime._environment.handlers.export import _prepare_export
from cli_agent.runtime._execution import ExecutionHandle

_CommandPreparer = Callable[[_ExecutionRequest, _CommandContext], ExecutionHandle]
_ParallelSafety = bool | Callable[[ShellParseResult], bool]


class _Command(ABC):
    """Describe one Runtime-owned command family."""

    name: str | None
    isolated: bool

    @abstractmethod
    def matches(self, command: ShellParseResult) -> bool:
        """Return whether this command owns the parsed command."""

    @abstractmethod
    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Return whether this command may enter a parallel batch."""

    @abstractmethod
    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
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
        consumes_stdin: bool = False,
    ) -> None:
        if (
            not name
            or name.strip() != name
            or any(character.isspace() for character in name)
        ):
            raise ValueError("custom command name must be one non-empty token")
        if not isinstance(isolated, bool):
            raise TypeError("custom command isolated must be a bool")
        if not isinstance(parallel_safe, bool) and not callable(parallel_safe):
            raise TypeError("custom command parallel_safe must be a bool or callable")
        if not isinstance(consumes_stdin, bool):
            raise TypeError("custom command consumes_stdin must be a bool")
        self.name = name
        self.isolated = isolated
        self._prepare = prepare
        self._parallel_safe = parallel_safe
        self.consumes_stdin = consumes_stdin

    def matches(self, command: ShellParseResult) -> bool:
        """Return whether the first command token matches this command name."""

        return command.command_head == self.name

    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Evaluate this command's fixed or command-specific schedule fact."""

        value = self._parallel_safe
        return value(command) if callable(value) else value

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        """Construct the command execution without starting it.

        A custom command that does not consume standard input rejects any
        non-``None`` stdin instead of silently dropping the input.
        """

        if request.stdin is not None and not self.consumes_stdin:
            return _text_execution(
                f"`{self.name}` does not consume exec stdin; "
                "omit stdin or use a shell command\n",
                success=False,
            )
        return self._prepare(request, context)


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

    def matches(self, command: ShellParseResult) -> bool:
        """Return true because Shell is the fallback for every unmatched command."""

        del command
        return True

    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Return whether the parsed Shell command is trusted for parallel use."""

        return bool(
            command.syntax_valid
            and command.executable_basename in self._parallel_commands
            and not command.contains_shell_composition
        )

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        """Construct the Shell execution without starting it."""

        return self._prepare(request, context)


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
        command: ShellParseResult,
    ) -> _CustomCommand | None:
        """Return the custom command selected by the command-head rule."""

        head = command.command_head
        if head is None:
            return None
        return self._commands.get(head)


def _builtin_custom_commands(
    filesystem: _WorkspaceFilesystem | None = None,
) -> tuple[_CustomCommand, ...]:
    """Return the built-in Session commands installed in every Kernel."""

    return (
        _CustomCommand(
            name="cd",
            prepare=_prepare_cd(filesystem),
            isolated=False,
        ),
        _CustomCommand(name="export", prepare=_prepare_export, isolated=False),
    )
