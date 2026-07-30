"""AEP-style registry for Runtime-trusted custom commands."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cli_agent.runtime._environment.command_parser import CommandParseResult

if TYPE_CHECKING:
    from cli_agent.runtime._environment.drivers.base import (
        _DriverContext,
        _DriverExecution,
    )


class _CustomCommandPreparer(Protocol):
    """Prepare one invocation of a registered custom command."""

    def __call__(
        self,
        command: CommandParseResult,
        context: _DriverContext,
    ) -> _DriverExecution:
        """Return a cancellable execution without starting requested work."""


class _CustomSchedulingRule(Protocol):
    """Classify one invocation using only Runtime-trusted command metadata."""

    def __call__(self, command: CommandParseResult) -> bool:
        """Return whether this exact invocation may join a parallel batch."""


@dataclass(frozen=True, slots=True)
class _CustomCommandSpec:
    """Runtime-trusted implementation and scheduling facts for one command."""

    name: str
    prepare: _CustomCommandPreparer
    parallel_safe: bool | _CustomSchedulingRule = False

    def __post_init__(self) -> None:
        normalized = self.name.strip()
        if not normalized or normalized != self.name or any(
            character.isspace() for character in normalized
        ):
            raise ValueError("custom command name must be one non-empty token")

    def is_parallel_safe(self, command: CommandParseResult) -> bool:
        """Evaluate the trusted scheduling rule for this invocation."""

        rule = self.parallel_safe
        return rule(command) if callable(rule) else rule


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
