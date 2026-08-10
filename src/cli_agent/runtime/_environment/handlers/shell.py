"""Shell command-family execution handler."""

from __future__ import annotations

from cli_agent.runtime._backend import _BackendWorkspace, _ShellExecutionRequest
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
    _PreparedExecution,
)


class _ShellHandler:
    """Prepare ordinary commands as backend-neutral Shell requests."""

    def __init__(self, backend: _BackendWorkspace | None = None) -> None:
        self._backend = backend

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> _PreparedExecution:
        backend = self._backend
        if backend is None:
            raise RuntimeError("Shell handler requires a Backend Workspace")
        stdin = request.stdin
        return backend.prepare_shell(
            _ShellExecutionRequest(
                command=request.command,
                cwd=str(context.cwd),
                environment=context.environment,
                input_data=stdin.encode("utf-8") if stdin is not None else None,
            )
        )
