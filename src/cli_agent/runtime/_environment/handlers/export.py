"""Session custom-environment mutation command handler."""

from __future__ import annotations

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution


def _prepare_export(
    command: ShellParseResult,
    context: _CommandContext,
) -> _InlineExecution:
    """Prepare an environment mutation without applying it before execution."""

    args = command.tokens[1:]

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        if not args:
            text = "\n".join(
                f"{key}={value}" for key, value in context.environment.items()
            )
            await output.write(
                "stdout",
                (text or "(no custom env vars)").encode(),
            )
            return _ExecutionOutcome.exited()

        assignments: list[tuple[str, str]] = []
        for item in args:
            if "=" not in item:
                await output.write(
                    "stderr",
                    f"Invalid format: {item}, expected KEY=VALUE\n".encode(),
                )
                return _ExecutionOutcome.failed(1)
            assignments.append(tuple(item.split("=", 1)))

        context.environment.update(assignments)
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)
