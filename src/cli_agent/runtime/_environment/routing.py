"""Map allowed decisions to Runtime-trusted execution routes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli_agent.runtime._environment.policy import (
    CommandParseResult,
    ExecutionDecision,
)


class _ExecutionLane(Enum):
    SHELL = "shell"


class _DriverKind(Enum):
    SHELL = "shell"


@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    lane: _ExecutionLane
    driver: _DriverKind


def _route_decision(decision: ExecutionDecision) -> _ExecutionRoute:
    command = _allowed_command(decision)
    if command.operation == "shell.execute":
        return _ExecutionRoute(
            lane=_ExecutionLane.SHELL,
            driver=_DriverKind.SHELL,
        )
    raise RuntimeError(f"unsupported Execution operation: {command.operation}")


def _allowed_command(decision: ExecutionDecision) -> CommandParseResult:
    if not decision.allowed:
        raise RuntimeError("execution plane received a denied decision")
    return decision.parse_result
