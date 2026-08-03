"""Resolve parsed commands to unified Runtime command contracts."""

from __future__ import annotations

from dataclasses import dataclass

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.commands import (
    _Command,
    _CustomCommandRegistry,
    _ShellCommand,
)


@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    """Bind one parsed command to its selected Command and schedule fact."""

    command: _Command
    parallel_safe: bool

    def __post_init__(self) -> None:
        if not isinstance(self.parallel_safe, bool):
            raise TypeError("execution route parallel_safe must be a bool")


class _CommandRouter:
    """Prefer registered custom commands and otherwise use Shell fallback."""

    def __init__(
        self,
        *,
        shell_command: _ShellCommand,
        custom_registry: _CustomCommandRegistry,
    ) -> None:
        self._shell_command = shell_command
        self._custom_registry = custom_registry

    def resolve(self, command: ShellParseResult) -> _ExecutionRoute:
        """Select one Command and its schedule fact without performing work."""

        selected = self._custom_registry.resolve(command)
        if selected is None:
            selected = self._shell_command

        return _ExecutionRoute(
            command=selected,
            parallel_safe=selected.parallel_safe(command),
        )
