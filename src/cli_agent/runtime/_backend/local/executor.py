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
import keyword
import os
import posixpath

from cli_agent.runtime._backend.facts import _ToolBinding, _ToolExecutionRequest
from cli_agent.runtime._backend.local.backend import _LocalBackendWorkspace
from cli_agent.runtime._backend.local.filesystem import _resolve_path
from cli_agent.runtime._backend.local.shell import (
    _ProcessExecution,
    _tool_worker_spawner,
)
from cli_agent.runtime._capability.deployment import (
    DeploymentSnapshot,
    StaleDeploymentError,
    verify_deployment,
)
from cli_agent.runtime._environment.handlers.base import _CommandContext
from cli_agent.runtime._environment.handlers.executions import _text_execution
from cli_agent.runtime._execution import ExecutionHandle

_TOOLS_DIRECTORY = "tools/"


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
    ) -> None:
        self._backend = backend
        self._workspace_id = workspace_id
        self._revision = revision
        self._deployment = deployment

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
        invalid_binding = _invalid_binding(request.bindings)
        if invalid_binding is not None:
            return _text_execution(
                f"Tool environment is unavailable: {invalid_binding}\n",
                success=False,
            )
        runtime = self._backend._tool_runtime
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
        payload = json.dumps(
            {
                "code": request.code,
                "workspace": self._backend.root,
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
        environment = {
            **self._backend.execution_base_environment(),
            **context.environment,
        }
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

    def _ensure_open(self) -> None:
        """Reject every operation after the Backend Workspace closes."""

        self._backend._ensure_open()


def _invalid_binding(bindings: tuple[_ToolBinding, ...]) -> str | None:
    """Return the first invalid binding, or None when every binding is safe.

    Bindings are the requested capabilities of one Tool run: names must be
    non-keyword Python identifiers and paths must stay inside the logical
    Tools tree, so the worker can never be steered outside the materialized
    Tools directory.
    """

    for binding in bindings:
        name = binding.name
        path = binding.path
        if (
            not isinstance(name, str)
            or not name
            or not name.isidentifier()
            or keyword.iskeyword(name)
        ):
            return f"invalid Tool binding: {name!r}"
        if (
            not isinstance(path, str)
            or posixpath.isabs(path)
            or not path.startswith(_TOOLS_DIRECTORY)
            or ".." in posixpath.normpath(path).split("/")
        ):
            return f"invalid Tool path: {path!r}"
    return None
