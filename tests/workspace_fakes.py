"""Workspace fakes for Kernel and Source contract tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cli_agent.runtime._backend import (
    Backend,
    _ShellExecutionRequest,
    _WorkspaceFilesystem,
)
from cli_agent.runtime._backend.local import _LocalBackendWorkspace
from cli_agent.runtime._execution import ExecutionHandle


class _KernelWorkspace:
    """Minimal logical Workspace around one live test Backend."""

    def __init__(
        self,
        backend: Backend,
        *,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.id = "test:00000000000000000000000000000000"
        self.root = backend.root
        self.filesystem: _WorkspaceFilesystem = backend.filesystem
        self.backend = backend
        self.base_environment = dict(base_environment or {})

    def prepare_shell(self, request: _ShellExecutionRequest) -> ExecutionHandle:
        return self.backend.prepare_shell(request)

    async def flush(self) -> None:
        await self.backend.flush()

    async def close(self) -> None:
        await self.backend.close()


def _kernel_workspace(
    root: str | Path,
    backend: Backend | None = None,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> _KernelWorkspace:
    """Return a logical Workspace suitable for one Kernel contract test."""

    if backend is None:
        path = Path(root).resolve()
        backend = _LocalBackendWorkspace(path, dict(base_environment or {}))
    return _KernelWorkspace(backend, base_environment=base_environment)
