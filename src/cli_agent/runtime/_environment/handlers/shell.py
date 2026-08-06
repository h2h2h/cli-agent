"""Shell command-family execution handler."""

from __future__ import annotations

from cli_agent.runtime._backend import _BackendWorkspace, _ShellExecutionRequest
from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _PreparedExecution,
)


class _ShellHandler:
    """Prepare ordinary commands as backend-neutral Shell requests."""

    def __init__(self, backend: _BackendWorkspace | None = None) -> None:
        self._backend = backend

    def prepare(
        self,
        command: ShellParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        backend = self._backend
        if backend is None:
            raise RuntimeError("Shell handler requires a Backend Workspace")
        return backend.prepare_shell(
            _ShellExecutionRequest(
                command=command,
                cwd=str(context.cwd),
                environment=context.environment,
            )
        )
