"""Shell command-family execution driver."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from cli_agent.runtime._environment.drivers.base import (
    _DriverContext,
    _DriverExecution,
    _ExecutionOutcome,
    _ExecutionOutput,
)
from cli_agent.runtime._environment.drivers.executions import _ProcessExecution
from cli_agent.runtime.capability.command_parser import CommandParseResult

if TYPE_CHECKING:
    from cli_agent.runtime.capability.view import _CapabilityView


class _ShellDriver:
    """Prepare ordinary commands for execution by a child Shell."""

    def __init__(self, capability_view: _CapabilityView | None = None) -> None:
        self._capability_view = capability_view

    def prepare(
        self,
        command: CommandParseResult,
        context: _DriverContext,
    ) -> _DriverExecution:
        child_env = dict(os.environ) | context.environment

        async def spawn_shell() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_shell(
                command.raw_command,
                cwd=context.cwd,
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )

        process = _ProcessExecution(spawn_shell)
        if self._capability_view is None:
            return process
        return _CapabilityShellExecution(
            process,
            self._capability_view,
            command,
            context,
        )


class _CapabilityShellExecution:
    """Run one Shell process inside Capability View mutation preparation."""

    def __init__(
        self,
        process: _ProcessExecution,
        capability_view: _CapabilityView,
        command: CommandParseResult,
        context: _DriverContext,
    ) -> None:
        self._process = process
        self._capability_view = capability_view
        self._command = command
        self._cwd = context.cwd
        self._cancel_requested = False

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        async with self._capability_view.prepare_shell(
            self._command,
            self._cwd,
            cancelled=lambda: self._cancel_requested,
        ) as prepared:
            if not prepared:
                return _ExecutionOutcome.killed()
            return await self._process.run(output)

    async def cancel(self) -> None:
        self._cancel_requested = True
        await self._process.cancel()
