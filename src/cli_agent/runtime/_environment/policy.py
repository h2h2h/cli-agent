"""Private execution admission types and the first Host-owned policy."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Validated values from one Session-scoped ``exec`` call."""

    command: str
    wait_ms: int
    output_limit: int


@dataclass(frozen=True, slots=True)
class CommandAnalysis:
    """Facts established without performing the requested operation."""

    executable_basename: str | None
    tokenization_succeeded: bool


@dataclass(frozen=True, slots=True)
class ExecutionPlanCandidate:
    """Shell execution configuration before authorization."""

    operation: str
    command: str
    cwd: Path
    wait_ms: int
    output_limit: int
    analysis: CommandAnalysis


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Immediate allow or deny decision returned by execution policy."""

    allowed: bool
    rule_id: str
    reason: str | None = None

    @classmethod
    def allow(cls, rule_id: str = "default.allow") -> PolicyDecision:
        return cls(allowed=True, rule_id=rule_id)

    @classmethod
    def deny(cls, *, rule_id: str, reason: str) -> PolicyDecision:
        return cls(allowed=False, rule_id=rule_id, reason=reason)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Immutable boundary between control and execution planes."""

    operation: str
    command: str
    cwd: Path
    wait_ms: int
    output_limit: int
    policy_rule_id: str


class ExecutionPolicy(Protocol):
    """Host-owned admission policy snapshotted by one Environment Kernel."""

    async def authorize(
        self,
        candidate: ExecutionPlanCandidate,
    ) -> PolicyDecision:
        """Allow or deny one candidate without performing its operation."""


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

    async def authorize(
        self,
        candidate: ExecutionPlanCandidate,
    ) -> PolicyDecision:
        executable = candidate.analysis.executable_basename
        if executable is not None and executable in self._denied_executables:
            return PolicyDecision.deny(
                rule_id=f"shell.deny-executable.{executable}",
                reason=f"direct invocation of {executable!r} is denied by policy",
            )
        return PolicyDecision.allow()


def build_shell_candidate(
    request: ExecutionRequest,
    *,
    cwd: Path,
) -> ExecutionPlanCandidate:
    """Inspect a Shell request without executing or rewriting it."""

    return ExecutionPlanCandidate(
        operation="shell.execute",
        command=request.command,
        cwd=cwd,
        wait_ms=request.wait_ms,
        output_limit=request.output_limit,
        analysis=_inspect_direct_executable(request.command),
    )


def freeze_plan(
    candidate: ExecutionPlanCandidate,
    decision: PolicyDecision,
) -> ExecutionPlan:
    """Freeze an allowed candidate for the execution plane."""

    if not decision.allowed:
        raise ValueError("cannot freeze a denied execution candidate")
    return ExecutionPlan(
        operation=candidate.operation,
        command=candidate.command,
        cwd=candidate.cwd,
        wait_ms=candidate.wait_ms,
        output_limit=candidate.output_limit,
        policy_rule_id=decision.rule_id,
    )


def _inspect_direct_executable(command: str) -> CommandAnalysis:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return CommandAnalysis(
            executable_basename=None,
            tokenization_succeeded=False,
        )

    executable = Path(tokens[0]).name if tokens else None
    return CommandAnalysis(
        executable_basename=executable,
        tokenization_succeeded=True,
    )
