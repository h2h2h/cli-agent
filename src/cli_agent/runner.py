"""Run one cli-agent task through the public Runtime."""

from __future__ import annotations

from typing import TextIO
from uuid import uuid4

from cli_agent.config import CliConfig
from cli_agent.presentation import render_event
from cli_agent.runtime import (
    AgentRuntime,
    ModelCompletion,
    ModelProvider,
    UserMessage,
)


async def run_agent(
    config: CliConfig,
    provider: ModelProvider,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run and present one task, returning a process-style exit code."""

    session_id = uuid4().hex
    completed = False

    async with AgentRuntime.open(
        workspace=config.workspace,
        provider=provider,
    ) as runtime:
        try:
            async for event in runtime.run_turn(
                session_id,
                UserMessage.text(config.task),
            ):
                render_event(
                    event,
                    stdout=stdout,
                    stderr=stderr,
                )
                if isinstance(event, ModelCompletion):
                    completed = True
        finally:
            await runtime.close_session(session_id)

    if not completed:
        print(
            "cli-agent: model stream ended without a completion",
            file=stderr,
            flush=True,
        )
        return 1
    return 0
