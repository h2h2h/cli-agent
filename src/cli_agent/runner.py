"""Run cli-agent turns through the public Runtime."""

from __future__ import annotations

import asyncio
from typing import TextIO
from uuid import uuid4

from cli_agent.config import CliConfig
from cli_agent.presentation import render_diagnostic, render_event, render_prompt
from cli_agent.runtime import (
    AgentRuntime,
    ApprovalResponse,
    ExecutionApprovalRequest,
    ModelCompletion,
    ModelProvider,
    TextDelta,
    UserMessage,
)


async def run_agent(
    config: CliConfig,
    provider: ModelProvider,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run and present one-shot or interactive Agent turns."""

    session_id = uuid4().hex

    async with await AgentRuntime.open(
        workspace=config.workspace,
        repertoire=config.repertoire,
        provider=provider,
        execution_approver=_TerminalExecutionApprover(
            stdin=stdin,
            stderr=stderr,
        ),
        on_diagnostic=lambda diagnostic: render_diagnostic(
            diagnostic,
            stderr=stderr,
        ),
    ) as runtime:
        try:
            if config.task is not None:
                completed, _ = await _run_turn(
                    runtime,
                    session_id,
                    config.task,
                    stdout=stdout,
                    stderr=stderr,
                    separate_diagnostics=False,
                )
                return _turn_exit_code(completed, stderr=stderr)

            while True:
                task = _read_interactive_task(stdin=stdin, stderr=stderr)
                if task is None:
                    return 0

                completed, needs_newline = await _run_turn(
                    runtime,
                    session_id,
                    task,
                    stdout=stdout,
                    stderr=stderr,
                    separate_diagnostics=True,
                )
                if needs_newline:
                    print(file=stdout, flush=True)
                exit_code = _turn_exit_code(completed, stderr=stderr)
                if exit_code != 0:
                    return exit_code
        finally:
            await runtime.close_session(session_id)


async def _run_turn(
    runtime: AgentRuntime,
    session_id: str,
    task: str,
    *,
    stdout: TextIO,
    stderr: TextIO,
    separate_diagnostics: bool,
) -> tuple[bool, bool]:
    completed = False
    last_text_has_newline = True

    async for event in runtime.run_turn(
        session_id,
        UserMessage.text(task),
    ):
        if (
            separate_diagnostics
            and not isinstance(event, TextDelta)
            and not last_text_has_newline
        ):
            print(file=stdout, flush=True)
            last_text_has_newline = True
        render_event(
            event,
            stdout=stdout,
            stderr=stderr,
        )
        if isinstance(event, ModelCompletion):
            completed = True
        elif isinstance(event, TextDelta) and event.text:
            last_text_has_newline = event.text.endswith("\n")

    return completed, not last_text_has_newline


def _read_interactive_task(*, stdin: TextIO, stderr: TextIO) -> str | None:
    while True:
        prompted = stdin.isatty()
        if prompted:
            render_prompt(stderr=stderr)

        line = stdin.readline()
        if line == "":
            if prompted:
                print(file=stderr, flush=True)
            return None

        task = line.strip()
        if task == ":q":
            return None
        if task:
            return task


def _turn_exit_code(completed: bool, *, stderr: TextIO) -> int:
    if not completed:
        print(
            "cli-agent: model stream ended without a completion",
            file=stderr,
            flush=True,
        )
        return 1
    return 0


class _TerminalExecutionApprover:
    """Resolve Runtime ASK evaluations through the Reference CLI streams."""

    def __init__(self, *, stdin: TextIO, stderr: TextIO) -> None:
        self._stdin = stdin
        self._stderr = stderr

    async def approve(
        self,
        request: ExecutionApprovalRequest,
    ) -> ApprovalResponse:
        print(
            f"[approval] {request.reason}",
            file=self._stderr,
            flush=True,
        )
        print(
            f"  command: {request.raw_command}",
            file=self._stderr,
            flush=True,
        )
        self._stderr.write("Allow once? [y/N] ")
        self._stderr.flush()
        response = await asyncio.to_thread(self._stdin.readline)
        if not self._stdin.isatty():
            print(file=self._stderr, flush=True)
        if response.strip().casefold() in {"y", "yes"}:
            return ApprovalResponse.ALLOW
        return ApprovalResponse.DENY
