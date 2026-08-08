"""Local Backend: Host-filesystem Workspace and Host subprocess mechanics.

The Local Backend is the reference RFC-0012 implementation: it owns the Host
``Path`` used for filesystem operations, the Host ambient environment merge
strategy, every ordinary Shell subprocess, the file-level Capability View
materialization, the Tool Runtime (Workspace-private venv, materialized
worker, worker subprocesses), and the Workspace MCP Runtime (server
discovery and the worker-side invocation binding), while exposing only
backend-neutral facts and contracts.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from cli_agent.runtime._backend.facts import (
    _CapabilitySource,
    _CapabilityState,
    _ShellExecutionRequest,
    _ToolExecutionRequest,
    _ToolRuntimeStatus,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.local.filesystem import (
    _LocalWorkspaceFilesystem,
    _resolve_path,
)
from cli_agent.runtime._backend.local.mcp_runtime import _LocalMCPRuntime
from cli_agent.runtime._backend.local.shell import (
    _LocalShellExecution,
    _ProcessExecution,
    _tool_worker_spawner,
)
from cli_agent.runtime._backend.local.tool_runtime import _LocalToolRuntime
from cli_agent.runtime._backend.local.view import (
    _LocalCapabilityView,
    _UnimplementedCapabilityView,
)
from cli_agent.runtime._backend.protocol import (
    _BoundCapabilityView,
    _WorkspaceMCPRuntime,
)
from cli_agent.runtime._capability.workspace import _load_workspace_env
from cli_agent.runtime._environment.handlers.base import _PreparedExecution
from cli_agent.runtime._environment.handlers.executions import _text_execution


class _LocalBackend:
    """Open one Host-filesystem Local Backend Workspace."""

    async def open_workspace(
        self,
        source: _WorkspaceSource,
        capability_source: _CapabilitySource,
        capability_state: _CapabilityState,
    ) -> _LocalBackendWorkspace:
        """Open the Local Workspace; any open failure must fail closed.

        Args:
            source (`_WorkspaceSource`):
                Host Workspace root and environment file.
            capability_source (`_CapabilitySource`):
                Host Capability lower input; the Local Bound Capability View
                is materialized from it during open.
            capability_state (`_CapabilityState`):
                Host persistent Capability state root; the materialized View
                and its whiteouts live under it.

        Returns:
            The opened Local Backend Workspace.

        Raises:
            ValueError: If the Workspace root or Repertoire is missing, or
                the environment file is unreadable.
        """

        root = source.root.resolve()
        if not root.is_dir():
            raise ValueError(f"workspace must be an existing directory: {root}")
        environment = _load_workspace_env(source.environment)
        view = _LocalCapabilityView.materialize(
            state_root=capability_state.root,
            repertoire=capability_source.repertoire,
        )
        return _LocalBackendWorkspace(root, environment, view)


class _LocalBackendWorkspace:
    """One live Local Backend Workspace on the Host filesystem."""

    def __init__(
        self,
        root: Path,
        environment: Mapping[str, str],
        capability_view: _LocalCapabilityView | None = None,
    ) -> None:
        self.root = str(root)
        self._root = root
        self._closed = False
        self.filesystem = _LocalWorkspaceFilesystem(
            root,
            capability_view,
            ensure_open=self._ensure_open,
        )
        self.capabilities: _BoundCapabilityView = (
            capability_view
            if capability_view is not None
            else _UnimplementedCapabilityView()
        )
        self.mcp: _WorkspaceMCPRuntime = _LocalMCPRuntime(
            self._capability_view_root,
            self.execution_base_environment,
            self._ensure_open,
        )
        self.workspace_environment = environment
        self._capability_view = capability_view
        self._tool_runtime: _LocalToolRuntime | None = None

    def execution_base_environment(self) -> Mapping[str, str]:
        """Return the Local execution base environment for child processes.

        The Host ambient environment is merged under the Workspace
        environment, so a Workspace value overrides the same Host variable.
        Handlers must not read ``os.environ`` themselves.
        """

        self._ensure_open()
        return {**os.environ, **self.workspace_environment}

    def _capability_view_root(self) -> Path | None:
        """Return the Bound Capability View root, if one is materialized."""

        self._ensure_open()
        view = self._capability_view
        return Path(view.root) if view is not None else None

    def _ensure_open(self) -> None:
        """Reject every operation after this Backend Workspace closes."""

        if self._closed:
            raise RuntimeError("Backend Workspace is closed")

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> _PreparedExecution:
        """Prepare one Shell execution without starting a subprocess."""

        self._ensure_open()
        return _LocalShellExecution(
            command=request.command,
            cwd=_resolve_path(self._root, request.cwd),
            environment={
                **self.execution_base_environment(),
                **request.environment,
            },
            mutation=self._capability_view,
        )

    def prepare_tool(
        self,
        request: _ToolExecutionRequest,
    ) -> _PreparedExecution:
        """Prepare one Tool worker execution inside this Workspace.

        The worker, its venv, and the effective Tools directory come from the
        reconciled Local Tool Runtime; the Host ambient environment is merged
        under the Session environment by the Backend. A missing or failed Tool
        Runtime fails this execution with text output; it never falls back to
        the Host Python.
        """

        self._ensure_open()
        runtime = self._tool_runtime
        if runtime is None:
            return _text_execution(
                "Tool environment is unavailable\n",
                success=False,
            )
        if (
            not runtime.available
            or runtime.python is None
            or runtime.worker is None
            or runtime.tools_directory is None
        ):
            return _text_execution(
                (runtime.error or "Tool environment is unavailable") + "\n",
                success=False,
            )

        cwd = _resolve_path(self._root, request.cwd)
        payload = json.dumps(
            {
                "code": request.code,
                "workspace": self.root,
                "cwd": str(cwd),
                "tools_directory": str(runtime.tools_directory),
                "binding_directory": str(runtime.root),
                "tool_paths": {
                    binding.name: binding.path for binding in request.bindings
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        python = runtime.python
        environment = {**self.execution_base_environment(), **request.environment}
        environment["VIRTUAL_ENV"] = str(runtime.root / ".venv")
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        bin_directory = str(python.parent)
        existing_path = environment.get("PATH")
        environment["PATH"] = (
            bin_directory
            if not existing_path
            else bin_directory + os.pathsep + existing_path
        )
        return _ProcessExecution(
            _tool_worker_spawner(python, runtime.worker, cwd, environment),
            input_data=payload,
        )

    async def reconcile_tool_runtime(self) -> _ToolRuntimeStatus:
        """Reconcile the Local Tool Runtime and return its availability.

        The Workspace-private venv and the materialized worker are created
        under the Bound Capability View root; dependency failures produce an
        unavailable status instead of raising.
        """

        self._ensure_open()
        view = self._capability_view
        if view is None:
            runtime = _LocalToolRuntime(
                root=self._root / ".workspace" / ".tool-environment",
                python=None,
                worker=None,
                tools_directory=None,
                error="Tool environment is unavailable: no Capability View",
            )
        else:
            runtime = await _LocalToolRuntime.reconcile(Path(view.root))
        self._tool_runtime = runtime
        return runtime.status

    async def flush(self) -> None:
        """Local Workspace changes are immediately durable; nothing to flush."""

        self._ensure_open()

    async def close(self) -> None:
        """Close this Workspace idempotently; no open resources yet."""

        if self._closed:
            return
        self._closed = True
        view = self._capability_view
        if view is not None:
            view.close()
