"""Local Backend: Host-filesystem Workspace and Host subprocess mechanics.

The Local Backend is the reference RFC-0012 implementation: it owns the Host
``Path`` used for filesystem operations, the Host ambient environment merge
strategy, and every ordinary Shell subprocess, while exposing only
backend-neutral facts and contracts.

Capability materialization and Tool execution are composed beside this
Backend through explicit target ports. The Backend therefore owns no
capability state and exposes only Workspace filesystem and Shell mechanics.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from cli_agent.runtime._backend.facts import (
    _ShellExecutionRequest,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.local.filesystem import (
    _LocalWorkspaceFilesystem,
    _resolve_path,
)
from cli_agent.runtime._backend.local.shell import _LocalShellExecution
from cli_agent.runtime._capability.workspace import _load_workspace_env
from cli_agent.runtime._execution import ExecutionHandle


class _LocalBackend:
    """Open one Host-filesystem Local Backend Workspace."""

    async def open_workspace(
        self,
        source: _WorkspaceSource,
    ) -> _LocalBackendWorkspace:
        """Open the Local Workspace; any open failure must fail closed.

        Args:
            source (`_WorkspaceSource`):
                Host Workspace root and environment file.

        Returns:
            The opened Local Backend Workspace.

        Raises:
            ValueError: If the Workspace root is missing or the environment
                file is unreadable.
        """

        root = source.root.resolve()
        if not root.is_dir():
            raise ValueError(f"workspace must be an existing directory: {root}")
        environment = _load_workspace_env(source.environment)
        return _LocalBackendWorkspace(root, environment)


class _LocalBackendWorkspace:
    """One live Local Backend Workspace on the Host filesystem."""

    def __init__(
        self,
        root: Path,
        environment: Mapping[str, str],
    ) -> None:
        self.root = str(root)
        self._root = root
        self._closed = False
        self.filesystem = _LocalWorkspaceFilesystem(
            root,
            ensure_open=self._ensure_open,
        )
        self.workspace_environment = environment

    def execution_base_environment(self) -> Mapping[str, str]:
        """Return the Local execution base environment for child processes.

        The Host ambient environment is merged under the Workspace
        environment, so a Workspace value overrides the same Host variable.
        Handlers must not read ``os.environ`` themselves.
        """

        self._ensure_open()
        return {**os.environ, **self.workspace_environment}

    def _ensure_open(self) -> None:
        """Reject every operation after this Backend Workspace closes."""

        if self._closed:
            raise RuntimeError("Backend Workspace is closed")

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> ExecutionHandle:
        """Prepare one Shell execution without starting a subprocess."""

        self._ensure_open()
        return _LocalShellExecution(
            command=request.command,
            cwd=_resolve_path(self._root, request.cwd),
            environment={
                **self.execution_base_environment(),
                **request.environment,
            },
            input_data=request.input_data,
        )

    async def flush(self) -> None:
        """Local Workspace changes are immediately durable; nothing to flush."""

        self._ensure_open()

    async def close(self) -> None:
        """Close this Workspace idempotently; no open resources yet."""

        if self._closed:
            return
        self._closed = True
