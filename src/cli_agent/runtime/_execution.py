"""Execution lifecycle contracts shared by every concrete execution.

An ``ExecutionHandle`` owns the resources, output, and termination of one
concrete execution, whether inline, filesystem, shell, or tool. Output
bytes are pushed to an ``ExecutionOutputSink`` and the terminal status is
expressed only through the single return value of ``run``: command
semantics are always an exit code, while failures of the execution
environment itself raise ``BackendExecutionError``.
"""

from __future__ import annotations

import signal
from typing import Literal, Protocol

_SIGNAL_EXIT_BASE = 128

_KILLED_BEFORE_START = _SIGNAL_EXIT_BASE + signal.SIGTERM


class ExitStatus(int):
    """Signal-normalized terminal exit code of one executed command.

    Exit codes are returned unchanged; a process terminated by signal
    ``N`` reports ``128 + N``. An execution killed before it started
    reports ``128 + SIGTERM``. There is no separate terminal status:
    whether a command failed is decided from the code alone.
    """


def _normalized_exit_status(code: int) -> ExitStatus:
    """Return one POSIX wait code as a signal-normalized ``ExitStatus``."""

    if code < 0:
        return ExitStatus(_SIGNAL_EXIT_BASE - code)
    return ExitStatus(code)


class BackendExecutionError(Exception):
    """A Backend mechanism failure before or around one execution.

    Raised when the execution environment itself fails (process or worker
    spawn, container creation, daemon disconnect). Command semantic
    failures are exit codes, never this error.
    """


class ExecutionOutputSink(Protocol):
    """Push normalized command output for one execution.

    The sink understands raw stdout and stderr bytes only; it never
    receives cursors, truncation facts, or model payloads.
    """

    async def write(self, stream: Literal["stdout", "stderr"], data: bytes) -> None:
        """Append one output chunk to the given stream."""


class ExecutionHandle(Protocol):
    """Own the resources and termination of one concrete execution."""

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        """Run exactly once; a second call is a Runtime invariant violation."""

    async def kill(self) -> None:
        """Terminate idempotently before, during, or after ``run``."""
