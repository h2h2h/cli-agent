"""Reserved Tool command handler backed by a fresh isolated worker."""

from __future__ import annotations

from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _ToolBinding,
    _ToolExecutionRequest,
)
from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.grammar import parse_tool_command
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
    _PreparedExecution,
)
from cli_agent.runtime._environment.handlers.executions import _text_execution


class _ToolHandler:
    """Prepare list, info, and run operations from trusted Tool facts.

    The Handler only converts trusted Tool facts into a backend-neutral
    ``_ToolExecutionRequest``; the Backend owns the worker Python, the worker
    path, the effective Tools directory, and the child environment. The
    Handler never reads Host Python paths, package resources, or the Host
    process environment.
    """

    def __init__(
        self,
        catalog: _ToolCatalog | None,
        backend: _BackendWorkspace | None,
    ) -> None:
        self._catalog = catalog
        self._backend = backend

    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Return the scheduling fact for one parsed Tools command."""

        if self._catalog is None:
            return False
        facts = parse_tool_command(command, self._catalog)
        if facts is None:
            return False
        if facts.operation in {"list", "inspect"}:
            return True
        return bool(
            facts.operation == "run"
            and facts.valid
            and facts.references
            and not facts.has_dynamic_references
            and all(
                reference.valid and reference.parallel_safe
                for reference in facts.references
            )
        )

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> _PreparedExecution:
        catalog = self._catalog
        if catalog is None:
            return _text_execution(
                "Tool catalog is unavailable\n",
                success=False,
            )
        facts = parse_tool_command(request.command, catalog)
        if facts is None:
            raise RuntimeError("Tool handler received an ordinary command")
        if facts.operation == "list":
            return _text_execution(catalog.render_index(), success=True)
        if facts.operation == "inspect" and facts.name is not None:
            text, success = catalog.render_info(facts.name)
            return _text_execution(text, success=success)
        if facts.operation != "run" or facts.code is None:
            return _text_execution(
                (facts.validation_error or "Invalid tools command") + "\n",
                success=False,
            )
        if not facts.valid:
            return _text_execution(
                (facts.validation_error or "Invalid Tool invocation") + "\n",
                success=False,
            )
        backend = self._backend
        if backend is None:
            return _text_execution(
                "Tool environment is unavailable\n",
                success=False,
            )
        return backend.prepare_tool(
            _ToolExecutionRequest(
                code=facts.code,
                cwd=context.cwd,
                environment=context.environment,
                bindings=tuple(
                    _ToolBinding(name=entry.name, path=entry.path)
                    for entry in catalog.valid_entries
                ),
            )
        )
