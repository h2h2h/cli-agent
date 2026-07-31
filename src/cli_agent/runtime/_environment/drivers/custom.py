"""AEP-style registered custom-command execution driver."""

from __future__ import annotations

from cli_agent.runtime._environment.commands.registry import (
    _CustomCommandRegistry,
    _CustomCommandSpec,
)
from cli_agent.runtime._environment.drivers.base import (
    _DriverContext,
    _DriverExecution,
)
from cli_agent.runtime.capability.command_parser import CommandParseResult


class _CustomDriver:
    """Delegate registered commands to their Runtime-trusted handlers."""

    def __init__(self, registry: _CustomCommandRegistry) -> None:
        self._registry = registry

    def resolve(self, command: CommandParseResult):
        """Return the exact registered command selected for this invocation."""

        return self._registry.resolve(command)

    def bind(self, spec: _CustomCommandSpec) -> _ResolvedCustomDriver:
        """Bind one resolved handler into an immutable per-Execution driver."""

        return _ResolvedCustomDriver(spec)


class _ResolvedCustomDriver:
    """Execute the exact Custom handler selected before admission."""

    def __init__(self, spec: _CustomCommandSpec) -> None:
        self._spec = spec

    def prepare(
        self,
        command: CommandParseResult,
        context: _DriverContext,
    ) -> _DriverExecution:
        return self._spec.prepare(command, context)
