"""Shared request contracts for Runtime-trusted command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cli_agent.runtime._capability.command_parser import ShellParseResult


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
