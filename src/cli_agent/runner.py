"""Run cli-agent turns through the public Runtime."""

from __future__ import annotations

import asyncio
from typing import TextIO
from uuid import uuid4

from cli_agent.config import CliConfig, build_context_policy
from cli_agent.presentation import render_diagnostic, render_event, render_prompt
from cli_agent.runtime import (
    AgentRuntime,
    ExecutionPolicy,
    ModelCompletion,
    ModelProvider,
    TextDelta,
    UserAnswer,
    UserMessage,
    UserQuestion,
)
from cli_agent.tui import TuiSession


async def run_agent(
    config: CliConfig,
    provider: ModelProvider,
    *,
    execution_policy: ExecutionPolicy | None = None,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run and present one-shot or interactive Agent turns."""

    session_id = uuid4().hex
    tui_session = _create_tui_session(
        config=config,
        stdin=stdin,
        stderr=stderr,
    )

    try:
        async with await AgentRuntime.open(
            workspace=config.workspace,
            repertoire=config.repertoire,
            provider=provider,
            execution_policy=execution_policy,
            context_policy=build_context_policy(config),
            user_interaction=_TerminalUserInteraction(
                stdin=stdin,
                stderr=stderr,
                tui_session=tui_session,
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
                    task = await _read_interactive_task(
                        stdin=stdin,
                        stderr=stderr,
                        tui_session=tui_session,
                    )
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
    finally:
        if tui_session is not None:
            await tui_session.close()


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


def _create_tui_session(
    *,
    config: CliConfig,
    stdin: TextIO,
    stderr: TextIO,
) -> TuiSession | None:
    if config.task is not None or not stdin.isatty() or not stderr.isatty():
        return None
    return TuiSession(stdin=stdin, stderr=stderr)


async def _read_interactive_task(
    *,
    stdin: TextIO,
    stderr: TextIO,
    tui_session: TuiSession | None = None,
) -> str | None:
    """Read one task without blocking Runtime background work."""

    if tui_session is not None:
        return await _read_tui_task(tui_session)

    while True:
        prompted = stdin.isatty()
        if prompted:
            render_prompt(stderr=stderr)

        line = await asyncio.to_thread(stdin.readline)
        if line == "":
            if prompted:
                print(file=stderr, flush=True)
            return None

        task = line.strip()
        if task == ":q":
            return None
        if task:
            return task


async def _read_tui_task(tui_session: TuiSession) -> str | None:
    while True:
        task = await tui_session.read_text("cli-agent> ")
        if task is None:
            return None

        stripped = task.strip()
        if stripped == ":q":
            return None
        if stripped:
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


class _TerminalUserInteraction:
    """Resolve Runtime questions through the Reference CLI streams."""

    def __init__(
        self,
        *,
        stdin: TextIO,
        stderr: TextIO,
        tui_session: TuiSession | None = None,
    ) -> None:
        self._stdin = stdin
        self._stderr = stderr
        self._tui_session = tui_session

    async def ask(self, request: UserQuestion) -> UserAnswer:
        print(
            f"[interaction] {request.prompt}",
            file=self._stderr,
            flush=True,
        )
        if self._tui_session is not None:
            allowed = await self._tui_session.confirm("Allow once? [y/N] ")
            if allowed:
                return UserAnswer(value="allow_once")
            return UserAnswer(value="deny")

        self._stderr.write("Allow once? [y/N] ")
        self._stderr.flush()
        response = await asyncio.to_thread(self._stdin.readline)
        if not self._stdin.isatty():
            print(file=self._stderr, flush=True)
        if response.strip().casefold() in {"y", "yes"}:
            return UserAnswer(value="allow_once")
        return UserAnswer(value="deny")
