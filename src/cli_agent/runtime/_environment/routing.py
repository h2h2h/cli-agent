"""Resolve allowed commands to Runtime-trusted drivers and scheduling rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli_agent.runtime._capability.command_parser import CommandParseResult
from cli_agent.runtime._environment.commands import _CustomCommandRegistry
from cli_agent.runtime._environment.drivers.base import _ExecutionDriver
from cli_agent.runtime._environment.drivers.tool import _ToolDriver
from cli_agent.runtime._environment.policy import ExecutionDecision


class _DriverKind(Enum):
    CUSTOM = "custom"
    SHELL = "shell"
    TOOL = "tool"


class _ExecutionLane(Enum):
    DEFAULT = "default"
    TOOL = "tool"


class _SchedulingClass(Enum):
    SERIAL = "serial"
    PARALLEL_SAFE = "parallel_safe"


@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    """One per-command route selected after Policy allows the exact command."""

    driver_kind: _DriverKind # CUSTOM / SHELL / TOOL
    scheduling: _SchedulingClass # SERIAL / PARALLEL_SAFE
    driver: _ExecutionDriver

    @property
    def lane(self) -> _ExecutionLane:
        return (
            _ExecutionLane.TOOL
            if self.driver_kind is _DriverKind.TOOL
            else _ExecutionLane.DEFAULT
        )


class _CommandRouter:
    """Prefer registered custom commands and otherwise fall back to Shell."""

    def __init__(
        self,
        *,
        shell_driver: _ExecutionDriver,
        custom_registry: _CustomCommandRegistry,
        tool_driver: _ToolDriver | None = None,
        parallel_commands: frozenset[str] = frozenset(),
        parallel_tools: frozenset[str] = frozenset(),
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
        self._shell_driver = shell_driver
        self._custom_registry = custom_registry
        self._tool_driver = tool_driver
        self._parallel_commands = parallel_commands
        self._parallel_tools = parallel_tools

    def route(self, decision: ExecutionDecision) -> _ExecutionRoute:
        """Resolve one final decision without performing its operation."""

        command = decision.parse_result
        if command.tool is not None:
            if self._tool_driver is None:
                raise RuntimeError("Tool commands are not available")
            return _ExecutionRoute(
                driver_kind=_DriverKind.TOOL,
                scheduling=self._tool_scheduling(command),
                driver=self._tool_driver,
            )

        custom = self._custom_registry.resolve(command)
        if custom is not None:
            return _ExecutionRoute(
                driver_kind=_DriverKind.CUSTOM,
                scheduling=(
                    _SchedulingClass.PARALLEL_SAFE
                    if custom.parallel_safe
                    else _SchedulingClass.SERIAL
                ),
                driver=custom,
            )

        return _ExecutionRoute(
            driver_kind=_DriverKind.SHELL,
            scheduling=self._shell_scheduling(command),
            driver=self._shell_driver,
        )

    def _tool_scheduling(
        self,
        command: CommandParseResult,
    ) -> _SchedulingClass:
        facts = command.tool
        if facts is None:
            raise RuntimeError("missing Tool command facts")
        if facts.operation in {"list", "inspect"}:
            return _SchedulingClass.PARALLEL_SAFE
        if (
            facts.operation == "run"
            and facts.valid
            and facts.references
            and not facts.has_dynamic_references
            and all(
                reference.valid and reference.name in self._parallel_tools
                for reference in facts.references
            )
        ):
            return _SchedulingClass.PARALLEL_SAFE
        return _SchedulingClass.SERIAL

    def _shell_scheduling(
        self,
        command: CommandParseResult,
    ) -> _SchedulingClass:
        if (
            command.tokenization_succeeded
            and command.executable_basename in self._parallel_commands
            and not command.contains_shell_composition
        ):
            return _SchedulingClass.PARALLEL_SAFE
        return _SchedulingClass.SERIAL
