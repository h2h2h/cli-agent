"""Shared contracts for Runtime-trusted command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from cli_agent.runtime._capability.command_parser import ShellParseResult

_TerminalStatus = Literal["exited", "failed", "killed"]


@dataclass(frozen=True, slots=True)
class _ExecutionRequest:
    """One immutable unit of routed work and its bound standard input.

    The request travels unchanged from admission through queuing, starting,
    and cancellation; the standard input is execution data bound to the
    command, not Session state.
    """

    command: ShellParseResult
    stdin: str | None = None


@dataclass(frozen=True, slots=True)
class _CommandContext:
    """Session state available to a command when an Execution starts."""

    workspace: str
    cwd: str
    environment: dict[str, str]
    set_cwd: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True)
class _ExecutionOutcome:
    """Backend-neutral terminal result returned by one prepared Execution."""

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
    """Append normalized command output to one Execution."""

    async def write(self, stream: Literal["stdout", "stderr"], data: bytes) -> None:
        """Append one output chunk or record that it was truncated."""


class _PreparedExecution(Protocol):
    """Own the resources and cancellation of one concrete execution."""

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        """Run exactly once and release owned resources before returning."""

    async def cancel(self) -> None:
        """Request cancellation idempotently."""
