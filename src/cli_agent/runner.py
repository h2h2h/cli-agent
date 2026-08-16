"""Run cli-agent turns through the public Runtime."""

from __future__ import annotations

import asyncio
from typing import TextIO

from cli_agent.config import CliConfig, build_context_policy
from cli_agent.errors import HostFacingError
from cli_agent.presentation import (
    render_command_usage,
    render_diagnostic,
    render_event,
    render_host_error,
    render_prompt,
    render_session_id,
    render_session_usage,
    render_sessions,
)
from cli_agent.presets import local_runtime_components
from cli_agent.runtime import (
    AgentRuntime,
    ExecutionPolicy,
    ModelCompletion,
    ModelProvider,
    RuntimeEvent,
    TextDelta,
    UserAnswer,
    UserMessage,
    UserQuestion,
    WorkspaceConfig,
)
from cli_agent.slash_commands import CommandAction, CommandInvocation, parse, specs
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

    session_id: str | None = None
    tui_session = _create_tui_session(
        config=config,
        stdin=stdin,
        stderr=stderr,
    )

    try:
        interaction = _TerminalUserInteraction(
            stdin=stdin,
            stderr=stderr,
            tui_session=tui_session,
        )
        components = local_runtime_components(
            interaction=interaction,
            context_policy=build_context_policy(config),
            policy=execution_policy,
            events=_TerminalEventSink(stderr),
        )
        async with await AgentRuntime.open(
            provider=provider,
            components=components,
            workspace_config=WorkspaceConfig(
                root=config.workspace,
                repertoire=config.repertoire,
            ),
        ) as runtime:
            try:
                try:
                    session = await runtime.new_session()
                except HostFacingError as exc:
                    render_host_error(exc, stderr=stderr)
                    return 1
                session_id = session.session_id
                if config.task is not None:
                    try:
                        completed, _ = await _run_turn(
                            runtime,
                            config.task,
                            stdout=stdout,
                            stderr=stderr,
                            separate_diagnostics=False,
                        )
                    except HostFacingError as exc:
                        render_host_error(exc, stderr=stderr)
                        return 1
                    return _turn_exit_code(completed, stderr=stderr)

                while True:
                    task = await _read_interactive_task(
                        stdin=stdin,
                        stderr=stderr,
                        tui_session=tui_session,
                    )
                    if task is None:
                        return 0
                    command = parse(task)
                    if command is not None and not command.valid and command.action in {
                        CommandAction.EXIT,
                        CommandAction.USAGE,
                    }:
                        command = None
                    if command is not None:
                        if not command.valid:
                            render_command_usage(command.usage, stderr=stderr)
                            continue
                        if command.action is CommandAction.EXIT:
                            return 0
                        try:
                            session_id = await _dispatch_command(
                                runtime,
                                command,
                                active_session_id=session_id,
                                stderr=stderr,
                            )
                        except HostFacingError as exc:
                            if command.action in {
                                CommandAction.NEW,
                                CommandAction.RESUME,
                            }:
                                session_id = None
                            render_host_error(exc, stderr=stderr)
                        continue

                    try:
                        completed, needs_newline = await _run_turn(
                            runtime,
                            task,
                            stdout=stdout,
                            stderr=stderr,
                            separate_diagnostics=True,
                        )
                    except HostFacingError as exc:
                        render_host_error(exc, stderr=stderr)
                        return 1
                    if needs_newline:
                        print(file=stdout, flush=True)
                    exit_code = _turn_exit_code(completed, stderr=stderr)
                    if exit_code != 0:
                        return exit_code
            finally:
                await runtime.detach_session()
    finally:
        if tui_session is not None:
            await tui_session.close()
        if session_id is not None:
            render_session_id(session_id, stderr=stderr)


async def _dispatch_command(
    runtime: AgentRuntime,
    command: CommandInvocation,
    *,
    active_session_id: str | None,
    stderr: TextIO,
) -> str | None:
    """Dispatch one validated slash command through Runtime lifecycle APIs."""

    action = command.action
    if action is CommandAction.USAGE:
        render_session_usage(runtime.session_usage(), stderr=stderr)
        return active_session_id
    if action is CommandAction.NEW:
        session = await runtime.new_session()
        return session.session_id
    if action is CommandAction.SESSIONS:
        sessions = await runtime.list_session_metadata(include_archived=True)
        render_sessions(
            sessions,
            active_session_id=active_session_id,
            stderr=stderr,
        )
        return active_session_id

    if action is CommandAction.RESUME:
        session_id = command.arguments[0]
        session = await runtime.resume_session(session_id)
        return session.session_id
    raise AssertionError(f"unhandled command action: {action!r}")


async def _run_turn(
    runtime: AgentRuntime,
    task: str,
    *,
    stdout: TextIO,
    stderr: TextIO,
    separate_diagnostics: bool,
) -> tuple[bool, bool]:
    completed = False
    last_text_has_newline = True

    async for event in runtime.run_turn(UserMessage.text(task)):
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
    return TuiSession(stdin=stdin, stderr=stderr, specs=specs)


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


class _TerminalEventSink:
    """Render structured Runtime events on the CLI diagnostic stream."""

    def __init__(self, stderr: TextIO) -> None:
        self._stderr = stderr

    def emit(self, event: RuntimeEvent) -> None:
        render_diagnostic(event, stderr=self._stderr)


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
