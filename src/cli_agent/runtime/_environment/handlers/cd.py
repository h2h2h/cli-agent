"""Session cwd mutation command handler.

The target is validated through the Workspace Filesystem, never through Host
``Path`` queries; the Backend interprets the path and reports directory
facts. On success the Backend cwd string is committed to the Kernel.
"""

from __future__ import annotations

from collections.abc import Callable

from cli_agent.runtime._backend import _FilesystemError, _WorkspaceFilesystem
from cli_agent.runtime._capability.command_parser import SimpleCommand
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
    _ExecutionOutput,
    _ExecutionRequest,
    _PreparedExecution,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution

_CdPreparer = Callable[[_ExecutionRequest, _CommandContext], _PreparedExecution]


def _prepare_cd(filesystem: _WorkspaceFilesystem | None) -> _CdPreparer:
    """Build the cd preparer bound to one Workspace Filesystem."""

    def prepare(
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> _PreparedExecution:
        command = request.command
        args = command.leading_arguments
        valid = (
            isinstance(command.root, SimpleCommand)
            and not command.contains_shell_composition
        )

        async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
            if not valid:
                await output.write("stderr", b"cd does not support Shell composition\n")
                return _ExecutionOutcome.failed(1)
            if filesystem is None:
                await output.write(
                    "stderr",
                    b"Workspace filesystem is unavailable\n",
                )
                return _ExecutionOutcome.failed(1)
            requested = context.workspace if not args else args[0]
            target = requested
            try:
                resolved = filesystem.resolve(requested, context.cwd)
                target = resolved.path
                metadata = await filesystem.stat(target)
            except _FilesystemError as exc:
                if exc.kind == "not_found":
                    await output.write(
                        "stderr",
                        f"Directory not found: {target}\n".encode(),
                    )
                elif exc.kind == "not_a_directory":
                    await output.write(
                        "stderr",
                        f"Not a directory: {requested}\n".encode(),
                    )
                else:
                    await output.write(
                        "stderr",
                        f"failed to change directory to {requested}: {exc}\n".encode(),
                    )
                return _ExecutionOutcome.failed(1)
            if metadata.kind != "directory":
                await output.write(
                    "stderr",
                    f"Not a directory: {target}\n".encode(),
                )
                return _ExecutionOutcome.failed(1)
            if context.set_cwd is None:
                return _ExecutionOutcome.failed(1)

            context.set_cwd(target)
            text = target
            if not resolved.within_workspace:
                text = (
                    f"{text}\n"
                    "Warning: You are outside the workspace. "
                    "Run `cd` to return to the workspace root."
                )
            await output.write("stdout", text.encode())
            return _ExecutionOutcome.exited()

        return _InlineExecution(execute)

    return prepare
