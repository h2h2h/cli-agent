"""Resolve authorized commands to unified Runtime command contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli_agent.runtime._capability.command_parser import CommandParseResult
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.grammar import parse_tool_command
from cli_agent.runtime._environment.commands import (
    _Command,
    _CustomCommand,
    _CustomCommandRegistry,
    _ShellCommand,
)
from cli_agent.runtime._environment.handlers.tools import _ToolHandler
from cli_agent.runtime._environment.policy import ExecutionDecision


class _DriverKind(Enum):
    """Legacy scheduler classification retained until the lane migration."""

    CUSTOM = "custom"
    SHELL = "shell"
    TOOL = "tool"


class _ExecutionLane(Enum):
    """Legacy scheduler lane retained until the global scheduler migration."""

    DEFAULT = "default"
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
        tool_handler: _ToolHandler | None = None,
        tool_catalog: _ToolCatalog | None = None,
        parallel_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._shell_command = shell_command
        self._custom_registry = custom_registry
        self._tool_catalog = tool_catalog
        self._tool_command = (
            None
            if tool_handler is None
            else _CustomCommand(
                name="tools",
                prepare=tool_handler.prepare,
                parallel_safe=self._tool_parallel_safe,
                isolated=True,
            )
        )
        self._parallel_tools = parallel_tools

    def route(self, decision: ExecutionDecision) -> _ExecutionRoute:
        """Resolve one final decision without performing its operation."""

        parsed = decision.parse_result
        command = self._custom_registry.resolve(parsed)
        if command is None and self._tool_command is not None:
            if self._tool_command.matches(parsed):
                command = self._tool_command
        if command is None:
            command = self._shell_command

        return _ExecutionRoute(
            command=command,
            parallel_safe=command.parallel_safe(parsed),
        )

    def _tool_parallel_safe(self, command: CommandParseResult) -> bool:
        if self._tool_catalog is None:
            return False
        facts = parse_tool_command(command, self._tool_catalog)
        if facts is None:
            return False
        if facts.operation in {"list", "inspect"}:
            return True
        return bool(
            facts.operation == "run"
            and facts.valid
            and facts.references
            and not facts.has_dynamic_references
            and all(
                reference.valid and reference.name in self._parallel_tools
                for reference in facts.references
            )
        )
