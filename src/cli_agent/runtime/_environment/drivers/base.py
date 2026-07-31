"""Shared contracts for Runtime-trusted execution drivers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from cli_agent.runtime._capability.command_parser import CommandParseResult

_TerminalStatus = Literal["exited", "failed", "killed"]


@dataclass(frozen=True, slots=True)
class _DriverContext:
    """Session state available to a Driver when an Execution starts."""

    workspace: Path
    cwd: Path
    environment: dict[str, str]
    set_cwd: Callable[[Path], None] | None = None


@dataclass(frozen=True, slots=True)
class _ExecutionOutcome:
    """Backend-neutral terminal result returned by one Driver Execution."""

    status: _TerminalStatus
    exit_code: int | None

    @classmethod
    def exited(cls, exit_code: int = 0) -> _ExecutionOutcome:
        return cls(status="exited", exit_code=exit_code)

    @classmethod
    def failed(cls, exit_code: int | None = None) -> _ExecutionOutcome:
        return cls(status="failed", exit_code=exit_code)

    @classmethod
    def killed(cls, exit_code: int | None = None) -> _ExecutionOutcome:
        return cls(status="killed", exit_code=exit_code)


class _ExecutionOutput(Protocol):
    """Append normalized Driver output to one Execution."""

    async def write(self, stream: Literal["stdout", "stderr"], data: bytes) -> None:
        """Append one output chunk or record that it was truncated."""


class _DriverExecution(Protocol):
    """Own the resources and cancellation of one concrete execution."""

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        """Run exactly once and release owned resources before returning."""

    async def cancel(self) -> None:
        """Request cancellation idempotently."""


class _ExecutionDriver(Protocol):
    """Prepare concrete Executions for one trusted command family."""

    def prepare(
        self,
        command: CommandParseResult,
        context: _DriverContext,
    ) -> _DriverExecution:
        """Prepare an Execution without starting work or mutating Session state."""
