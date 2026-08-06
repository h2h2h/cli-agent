"""Reserved Tool command handler backed by a fresh isolated worker."""

from __future__ import annotations

import asyncio
import json
import os
from importlib.resources import files

from cli_agent.runtime._backend.local import _ProcessExecution
from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.environment import _ToolEnvironment
from cli_agent.runtime._capability.tools.grammar import parse_tool_command
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _PreparedExecution,
)
from cli_agent.runtime._environment.handlers.executions import _text_execution


class _ToolHandler:
    """Prepare list, info, and run operations from trusted Tool facts."""

    def __init__(
        self,
        catalog: _ToolCatalog | None,
        environment: _ToolEnvironment | None,
    ) -> None:
        self._catalog = catalog
        self._environment = environment

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
        command: ShellParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        catalog = self._catalog
        if catalog is None:
            return _text_execution(
                "Tool catalog is unavailable\n",
                success=False,
            )
        facts = parse_tool_command(command, catalog)
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
        environment = self._environment
        if (
            environment is None
            or not environment.available
            or environment.python is None
        ):
            error = (
                "Tool environment is unavailable"
                if environment is None
                else environment.error or "Tool environment is unavailable"
            )
            return _text_execution(
                error + "\n",
                success=False,
            )

        payload = json.dumps(
            {
                "code": facts.code,
                "workspace": str(context.workspace),
                "cwd": str(context.cwd),
                "tools_directory": str(context.workspace / ".workspace" / "tools"),
                "tool_paths": {
                    entry.name: str(entry.path) for entry in catalog.valid_entries
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        python = environment.python
        worker = files("cli_agent.runtime._capability.tools").joinpath("worker.py")
        child_env = dict(os.environ) | context.environment
        child_env["VIRTUAL_ENV"] = str(environment.root / ".venv")
        child_env["PYTHONNOUSERSITE"] = "1"
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        bin_directory = str(python.parent)
        child_env["PATH"] = (
            bin_directory
            if not child_env.get("PATH")
            else bin_directory + os.pathsep + child_env["PATH"]
        )

        async def spawn_worker() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                str(python),
                str(worker),
                cwd=context.cwd,
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )

        return _ProcessExecution(spawn_worker, input_data=payload)
