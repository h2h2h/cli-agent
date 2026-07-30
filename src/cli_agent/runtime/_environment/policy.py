"""Private execution decision policy types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cli_agent.runtime._environment.command_parser import CommandParseResult


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """Immutable allow or deny decision for one exact parsed command."""

    allowed: bool
    parse_result: CommandParseResult
    rule_id: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.reason is not None:
            raise ValueError("an allowed execution decision cannot have a reason")
        if not self.allowed and not self.reason:
            raise ValueError("a denied execution decision must have a reason")

    @classmethod
    def allow(
        cls,
        parse_result: CommandParseResult,
        *,
        rule_id: str = "default.allow",
    ) -> ExecutionDecision:
        return cls(
            allowed=True,
            parse_result=parse_result,
            rule_id=rule_id,
        )

    @classmethod
    def deny(
        cls,
        parse_result: CommandParseResult,
        *,
        rule_id: str,
        reason: str,
    ) -> ExecutionDecision:
        return cls(
            allowed=False,
            parse_result=parse_result,
            rule_id=rule_id,
            reason=reason,
        )


class ExecutionPolicy(Protocol):
    """Host-owned decision policy shared by one Runtime's Session Kernels."""

    async def decide(
        self,
        command: CommandParseResult,
    ) -> ExecutionDecision:
        """Decide one parsed command without performing its operation."""


class DirectExecutableDenyPolicy:
    """Deny positively recognized direct executable invocations."""

    def __init__(self, denied_executables: frozenset[str] | None = None) -> None:
        configured = (
            frozenset({"rm"})
            if denied_executables is None
            else frozenset(denied_executables)
        )
        invalid = sorted(
            name for name in configured if not name or Path(name).name != name
        )
        if invalid:
            raise ValueError("denied executable names must be non-empty path basenames")
        self._denied_executables = configured

    async def decide(
        self,
        command: CommandParseResult,
    ) -> ExecutionDecision:
        executable = command.executable_basename
        if executable is not None and executable in self._denied_executables:
            return ExecutionDecision.deny(
                command,
                rule_id=f"shell.deny-executable.{executable}",
                reason=f"direct invocation of {executable!r} is denied by policy",
            )
        return ExecutionDecision.allow(command)
