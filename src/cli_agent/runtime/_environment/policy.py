"""Private command parsing and execution policy types."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandParseResult:
    """Validated command and facts established without executing it."""

    operation: str
    raw_command: str
    cwd: Path
    wait_ms: int
    output_limit: int
    executable_basename: str | None
    tokenization_succeeded: bool


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
    """Host-owned decision policy snapshotted by one Environment Kernel."""

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


def parse_shell_command(
    *,
    raw_command: str,
    cwd: Path,
    wait_ms: int,
    output_limit: int,
) -> CommandParseResult:
    """Parse one validated Shell command without executing or rewriting it."""

    try:
        tokens = shlex.split(raw_command, posix=True)
    except ValueError:
        executable = None
        tokenization_succeeded = False
    else:
        executable = Path(tokens[0]).name if tokens else None
        tokenization_succeeded = True

    return CommandParseResult(
        operation="shell.execute",
        raw_command=raw_command,
        cwd=cwd,
        wait_ms=wait_ms,
        output_limit=output_limit,
        executable_basename=executable,
        tokenization_succeeded=tokenization_succeeded,
    )
