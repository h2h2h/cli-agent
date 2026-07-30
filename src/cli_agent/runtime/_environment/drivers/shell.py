"""Shell command-family execution driver."""

from __future__ import annotations

import asyncio
import os

from cli_agent.runtime._environment.command_parser import CommandParseResult
from cli_agent.runtime._environment.drivers.base import (
    _DriverContext,
    _DriverExecution,
)
from cli_agent.runtime._environment.drivers.executions import _ProcessExecution


class _ShellDriver:
    """Prepare ordinary commands for execution by a child Shell."""

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

        return _ProcessExecution(spawn_shell)
