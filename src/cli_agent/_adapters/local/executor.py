"""Local ToolExecutor: convert deployed Tool requests into worker executions.

The default executor between the deployment plane and execution (RFC-0015):
it is composed by the Runtime with the active Workspace identity, the active
Capability snapshot revision, and the reconciled ``DeploymentSnapshot``.
Every ``prepare`` validates the deployment before any side effect, validates
the requested Tool bindings, and composes the worker payload; the worker
process itself starts only when the returned ``ExecutionHandle`` runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from cli_agent.runtime._backend.facts import _ToolExecutionRequest
from cli_agent.runtime._backend.local.backend import _LocalBackendWorkspace
from cli_agent.runtime._backend.local.filesystem import _resolve_path
from cli_agent.runtime._backend.local.shell import (
    _ProcessExecution,
    _tool_worker_spawner,
)
from cli_agent.runtime._capability.deployment import (
    DeploymentSnapshot,
    StaleDeploymentError,
    ToolRuntimeSnapshot,
    validate_tool_bindings,
    verify_deployment,
)
from cli_agent.runtime._environment.handlers.base import _CommandContext
from cli_agent.runtime._environment.handlers.executions import _text_execution
from cli_agent.runtime._execution import ExecutionHandle

if TYPE_CHECKING:
    from cli_agent.runtime._capability.snapshot import CapabilitySnapshot
    from cli_agent.runtime._workspace import Workspace


class _LocalToolExecutor:
    """Default ToolExecutor over the Local Tool Runtime in one Workspace.

    The worker, its venv, and the effective Tools directory come from the
    Local Tool Runtime the deployment attached to the Backend Workspace;
    the Host ambient environment is merged under the Session environment by
    the Backend. A stale, foreign, or incomplete deployment, an invalid
    binding, or a missing or failed Tool Runtime fails this execution with
    text output before any worker starts; it never falls back to the Host
    Python.
    """

    def __init__(
        self,
        backend: _LocalBackendWorkspace,
        *,
        workspace_id: str,
        revision: str,
        deployment: DeploymentSnapshot,
        runtime: ToolRuntimeSnapshot | None,
    ) -> None:
        self._backend = backend
        self._workspace_id = workspace_id
        self._revision = revision
        self._deployment = deployment
        self._runtime = runtime

    def prepare(
        self,
        request: _ToolExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        """Prepare one Tool worker execution without starting a process.

        Args:
            request (`_ToolExecutionRequest`):
                The Tool code and its logical bindings.
            context (`_CommandContext`):
                The Session execution facts (cwd and environment).

        Returns:
            One worker ``ExecutionHandle``; the worker process is created
            only when the handle runs.

        Raises:
            RuntimeError: If the Backend Workspace is closed.
        """

        self._ensure_open()
        deployment = self._deployment
        if not deployment.complete:
            return _text_execution(
                (deployment.error or "Tool environment is unavailable") + "\n",
                success=False,
            )
        try:
            verify_deployment(
                deployment,
                revision=self._revision,
                workspace_id=self._workspace_id,
            )
        except StaleDeploymentError as exc:
            return _text_execution(
                f"Tool environment is unavailable: {exc}\n",
                success=False,
            )
        invalid_binding = validate_tool_bindings(request.bindings)
        if invalid_binding is not None:
            return _text_execution(
                f"Tool environment is unavailable: {invalid_binding}\n",
                success=False,
            )
        runtime = self._runtime
        if (
            runtime is None
            or not runtime.available
            or runtime.python is None
            or runtime.worker is None
            or runtime.tools_directory is None
        ):
            return _text_execution(
                (
                    runtime.error
                    if runtime is not None and runtime.error is not None
                    else "Tool environment is unavailable"
                )
                + "\n",
                success=False,
            )

        cwd = _resolve_path(self._backend._root, context.cwd)
        assert runtime.binding_directory is not None
        payload = json.dumps(
            {
                "code": request.code,
                "workspace": self._backend.root,
                "cwd": str(cwd),
                "tools_directory": runtime.tools_directory,
                "binding_directory": runtime.binding_directory,
                "tool_paths": {
                    binding.name: binding.path for binding in request.bindings
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        python = Path(runtime.python)
        worker = Path(runtime.worker)
        environment = {
            **self._backend.execution_base_environment(),
            **context.environment,
        }
        environment["VIRTUAL_ENV"] = str(Path(runtime.binding_directory) / ".venv")
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
            _tool_worker_spawner(python, worker, cwd, environment),
            input_data=payload,
        )

    def _ensure_open(self) -> None:
        """Reject every operation after the Backend Workspace closes."""

        self._backend._ensure_open()


class _LocalToolExecutorFactory:
    """Create Local executors solely from deployment result facts."""

    def create(
        self,
        workspace: Workspace,
        snapshot: CapabilitySnapshot,
        deployment: DeploymentSnapshot,
    ) -> _LocalToolExecutor:
        backend = workspace.backend
        if not isinstance(backend, _LocalBackendWorkspace):
            raise ValueError("Local ToolExecutor requires a Local Backend")
        return _LocalToolExecutor(
            backend,
            workspace_id=workspace.id,
            revision=snapshot.revision,
            deployment=deployment,
            runtime=deployment.tool_runtime,
        )
