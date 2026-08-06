"""Session cwd mutation command handler."""

from __future__ import annotations

import os
from pathlib import Path

from cli_agent.runtime._capability.command_parser import ShellParseResult, SimpleCommand
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution


def _prepare_cd(
    command: ShellParseResult,
    context: _CommandContext,
) -> _InlineExecution:
    """Prepare a cwd mutation without applying it before execution starts."""

    args = command.leading_arguments
    valid = (
        isinstance(command.root, SimpleCommand)
        and not command.contains_shell_composition
    )

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        if not valid:
            await output.write("stderr", b"cd does not support Shell composition\n")
            return _ExecutionOutcome.failed(1)
        target = context.workspace if not args else _target_path(args[0], context.cwd)
        new_cwd = Path(os.path.normpath(str(target)))
        if not new_cwd.exists():
            await output.write(
                "stderr",
                f"Directory not found: {new_cwd}\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        if not new_cwd.is_dir():
            await output.write(
                "stderr",
                f"Not a directory: {new_cwd}\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        if context.set_cwd is None:
            return _ExecutionOutcome.failed(1)

        context.set_cwd(new_cwd)
        text = str(new_cwd)
        if not _is_within_workspace(new_cwd, context.workspace):
            text = (
                f"{text}\n"
                "Warning: You are outside the workspace. "
                "Run `cd` to return to the workspace root."
            )
        await output.write("stdout", text.encode())
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)


def _target_path(target: str, cwd: Path) -> Path:
    path = Path(target)
    if path.is_absolute() or target.startswith(("/", "\\")):
        return path
    if len(target) > 1 and target[1] == ":":
        return path
    return cwd / path


def _is_within_workspace(path: Path, workspace: Path) -> bool:
    path_norm = os.path.normcase(os.path.normpath(str(path)))
    workspace_norm = os.path.normcase(os.path.normpath(str(workspace)))
    try:
        common = os.path.commonpath([path_norm, workspace_norm])
    except ValueError:
        return False
    return common == workspace_norm
