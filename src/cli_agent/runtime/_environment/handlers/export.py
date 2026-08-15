"""Session custom-environment mutation command handler."""

from __future__ import annotations

from cli_agent.runtime._capability.command_parser import SimpleCommand
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._execution import ExecutionOutputSink, ExitStatus


def _prepare_export(
    request: _ExecutionRequest,
    context: _CommandContext,
) -> _InlineExecution:
    """Prepare an environment mutation without applying it before execution."""

    command = request.command
    args = command.leading_arguments
    valid = (
        isinstance(command.root, SimpleCommand)
        and not command.contains_shell_composition
    )

    async def execute(output: ExecutionOutputSink) -> ExitStatus:
        if not valid:
            await output.write("stderr", b"export does not support Shell composition\n")
            return ExitStatus(1)
        if not args:
            text = "\n".join(
                f"{key}={value}" for key, value in context.environment.items()
            )
            await output.write(
                "stdout",
                (text or "(no custom env vars)").encode(),
            )
            return ExitStatus(0)

        assignments: list[tuple[str, str]] = []
        for item in args:
            if "=" not in item:
                await output.write(
                    "stderr",
                    f"Invalid format: {item}, expected KEY=VALUE\n".encode(),
                )
                return ExitStatus(1)
            assignments.append(tuple(item.split("=", 1)))

        context.environment.update(assignments)
        return ExitStatus(0)

    return _InlineExecution(execute)
