"""Resolve authorized commands to unified Runtime command contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli_agent.runtime._environment.commands import (
    _Command,
    _CustomCommandRegistry,
    _ShellCommand,
)
from cli_agent.runtime._environment.policy import ExecutionDecision


class _DriverKind(Enum):
    """Legacy scheduler classification retained until the lane migration."""

    CUSTOM = "custom"
    SHELL = "shell"
    TOOL = "tool"


class _SchedulingClass(Enum):
    """Legacy scheduling labels retained for the in-progress migration."""

    SERIAL = "serial"
    PARALLEL_SAFE = "parallel_safe"


@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    """Bind an authorized command to its trusted scheduling decision."""

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

    def route(self, decision: ExecutionDecision) -> _ExecutionRoute:
        """Resolve one final decision without performing its operation."""

        parsed = decision.parse_result
        command = self._custom_registry.resolve(parsed)
        if command is None:
            command = self._shell_command

        return _ExecutionRoute(
            command=command,
            parallel_safe=command.parallel_safe(parsed),
        )
