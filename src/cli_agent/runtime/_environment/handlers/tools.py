"""Reserved Tool command handler backed by a fresh isolated worker."""

from __future__ import annotations

import asyncio
import json
import os
from importlib.resources import files

from cli_agent.runtime._capability.command_parser import CommandParseResult
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.environment import _ToolEnvironment
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
    _ExecutionOutput,
    _PreparedExecution,
)
from cli_agent.runtime._environment.handlers.executions import (
    _InlineExecution,
    _ProcessExecution,
)


class _ToolHandler:
    """Prepare list, info, and run operations from trusted Tool facts."""

    def __init__(
        self,
        catalog: _ToolCatalog,
        environment: _ToolEnvironment,
    ) -> None:
        self._catalog = catalog
        self._environment = environment

    def prepare(
        self,
        command: CommandParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        facts = command.tool
        if facts is None:
            raise RuntimeError("Tool handler received an ordinary command")
        if facts.operation == "list":
            return _text_execution(self._catalog.render_index(), success=True)
        if facts.operation == "inspect" and facts.name is not None:
            text, success = self._catalog.render_info(facts.name)
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
        if not self._environment.available or self._environment.python is None:
            return _text_execution(
                (self._environment.error or "Tool environment is unavailable") + "\n",
                success=False,
            )

        payload = json.dumps(
            {
                "code": facts.code,
                "workspace": str(context.workspace),
                "cwd": str(context.cwd),
                "tools_directory": str(context.workspace / ".workspace" / "tools"),
                "tool_paths": {
                    entry.name: str(entry.path)
                    for entry in self._catalog.valid_entries
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        python = self._environment.python
        worker = files("cli_agent.runtime._capability.tools").joinpath("worker.py")
        child_env = dict(os.environ) | context.environment
        child_env["VIRTUAL_ENV"] = str(self._environment.root / ".venv")
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


def _text_execution(text: str, *, success: bool) -> _InlineExecution:
    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        await output.write(
            "stdout" if success else "stderr",
            text.encode("utf-8"),
        )
        return (
            _ExecutionOutcome.exited()
            if success
            else _ExecutionOutcome.failed(1)
        )

    return _InlineExecution(execute)
