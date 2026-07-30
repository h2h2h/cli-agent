"""Resolve allowed commands to Runtime-trusted drivers and scheduling rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli_agent.runtime._environment.command_parser import CommandParseResult
from cli_agent.runtime._environment.drivers.base import _ExecutionDriver
from cli_agent.runtime._environment.drivers.custom import _CustomDriver
from cli_agent.runtime._environment.policy import ExecutionDecision


class _DriverKind(Enum):
    CUSTOM = "custom"
    SHELL = "shell"


class _SchedulingClass(Enum):
    SERIAL = "serial"
    PARALLEL_SAFE = "parallel_safe"


@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    """One per-command route selected after Policy allows the exact command."""

    driver_kind: _DriverKind
    scheduling: _SchedulingClass
    driver: _ExecutionDriver


class _CommandRouter:
    """Prefer registered custom commands and otherwise fall back to Shell."""

    def __init__(
        self,
        *,
        shell_driver: _ExecutionDriver,
        custom_driver: _CustomDriver,
        parallel_shell_commands: frozenset[str] = frozenset(),
    ) -> None:
        invalid = sorted(
            name
            for name in parallel_shell_commands
            if not name or name.strip() != name or "/" in name or "\\" in name
        )
        if invalid:
            raise ValueError(
                "parallel Shell command names must be non-empty executable basenames"
            )
        self._shell_driver = shell_driver
        self._custom_driver = custom_driver
        self._parallel_shell_commands = parallel_shell_commands

    def route(self, decision: ExecutionDecision) -> _ExecutionRoute:
        """Resolve one final decision without performing its operation."""

        command = decision.parse_result
        custom = self._custom_driver.resolve(command)
        if custom is not None:
            return _ExecutionRoute(
                driver_kind=_DriverKind.CUSTOM,
                scheduling=(
                    _SchedulingClass.PARALLEL_SAFE
                    if custom.is_parallel_safe(command)
                    else _SchedulingClass.SERIAL
                ),
                driver=self._custom_driver.bind(custom),
            )

        return _ExecutionRoute(
            driver_kind=_DriverKind.SHELL,
            scheduling=self._shell_scheduling(command),
            driver=self._shell_driver,
        )

    def _shell_scheduling(
        self,
        command: CommandParseResult,
    ) -> _SchedulingClass:
        if (
            command.tokenization_succeeded
            and command.executable_basename in self._parallel_shell_commands
            and not command.contains_shell_composition
        ):
            return _SchedulingClass.PARALLEL_SAFE
        return _SchedulingClass.SERIAL


def _route_decision(
    decision: ExecutionDecision,
    router: _CommandRouter,
) -> _ExecutionRoute:
    """Compatibility entrypoint for the Kernel's linear control path."""

    return router.route(decision)
