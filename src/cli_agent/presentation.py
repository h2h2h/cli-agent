"""Terminal presentation for provider-neutral model events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TextIO

from cli_agent.errors import HostFacingError
from cli_agent.runtime import (
    ModelCompletion,
    ModelEvent,
    RuntimeDiagnostic,
    SessionUsage,
    TextDelta,
    ToolCallReady,
)


class _SessionView(Protocol):
    """Metadata shape rendered by the Host without importing Runtime internals."""

    session_id: str
    workspace_id: str
    revision: int
    updated_at: datetime
    archived_at: datetime | None

_HOST_ERROR_MESSAGES = {
    "session_not_found": "Session was not found.",
    "session_archived": "Session is archived; unarchive it before resuming.",
    "session_conflict": "Session state changed; reload it before retrying.",
    "session_corrupted": "Session data is corrupted and cannot be used safely.",
    "session_persistence_failed": "Session persistence failed; check storage and retry.",
    "workspace_mismatch": "Session belongs to another Workspace and cannot be resumed here.",
    "runtime_state": "The Runtime state does not allow this operation.",
    "context_exhausted": "The Session context is exhausted and cannot continue.",
    "internal_error": "An internal Runtime error occurred; check diagnostics.",
}


def render_prompt(*, stderr: TextIO) -> None:
    """Render the interactive prompt."""

    prompt = _styled("cli-agent> ", "\033[1;36m", stream=stderr)
    stderr.write(prompt)
    stderr.flush()


def render_session_usage(usage: SessionUsage | None, *, stderr: TextIO) -> None:
    """Render the session-cumulative token usage to a Host stream.

    A missing usage snapshot is rendered as zero values.
    """

    if usage is None:
        usage = SessionUsage(input_tokens=0, output_tokens=0)
    text = f"[usage] input:{usage.input_tokens}, output:{usage.output_tokens}"
    print(_styled(text, "\033[2;32m", stream=stderr), file=stderr, flush=True)


def render_session_id(session_id: str, *, stderr: TextIO) -> None:
    """Render the current Session identifier when the CLI exits."""

    text = f"[session] {session_id}"
    print(_styled(text, "\033[2;36m", stream=stderr), file=stderr, flush=True)


def render_sessions(
    sessions: Sequence[_SessionView],
    *,
    active_session_id: str | None,
    stderr: TextIO,
) -> None:
    """Render safe Session metadata without loading conversation messages."""

    if not sessions:
        print("[sessions] none", file=stderr, flush=True)
        return
    for session in sessions:
        archived = "archived" if session.archived_at is not None else "active"
        current = " current" if session.session_id == active_session_id else ""
        print(
            "[session] "
            f"id={session.session_id} "
            f"workspace={session.workspace_id} "
            f"revision={session.revision} "
            f"updated_at={session.updated_at.isoformat(timespec='seconds')} "
            f"status={archived}{current}",
            file=stderr,
            flush=True,
        )


def render_command_usage(usage: str, *, stderr: TextIO) -> None:
    """Render stable help for a malformed slash command."""

    print(f"[command] usage: {usage}", file=stderr, flush=True)


def render_host_error(error: HostFacingError, *, stderr: TextIO) -> None:
    """Render a classified Host error with its automation-stable code."""

    message = _HOST_ERROR_MESSAGES.get(error.code, "The Host operation failed.")
    print(f"[error] code={error.code} {message}", file=stderr, flush=True)


def render_diagnostic(
    diagnostic: RuntimeDiagnostic,
    *,
    stderr: TextIO,
) -> None:
    """Render one Runtime Diagnostic to a Host stream."""

    text = f"[{diagnostic.kind}] {diagnostic.message}"
    print(_styled(text, "\033[1;33m", stream=stderr), file=stderr, flush=True)


def render_event(
    event: ModelEvent,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """Render one provider-neutral event."""

    if isinstance(event, TextDelta):
        stdout.write(event.text)
        stdout.flush()
        return

    if isinstance(event, ToolCallReady):
        diagnostic = _styled(
            f"[tool] {event.call.name}",
            "\033[1;35m",
            stream=stderr,
        )
        command = event.call.arguments.get("command")
        if event.call.name == "exec" and isinstance(command, str):
            styled_command = _styled(command, "\033[33m", stream=stderr)
            diagnostic += f": {styled_command}"
        print(diagnostic, file=stderr, flush=True)
        return

    if isinstance(event, ModelCompletion):
        diagnostic = f"[completion] reason={event.finish_reason}"
        if event.usage is not None:
            diagnostic += (
                f" usage=input:{event.usage.input_tokens}"
                f",output:{event.usage.output_tokens}"
                f",total:{event.usage.total_tokens}"
            )
        print(
            _styled(diagnostic, "\033[2;32m", stream=stderr),
            file=stderr,
            flush=True,
        )
        return

    raise TypeError(f"unsupported model event: {type(event).__name__}")


def _styled(text: str, style: str, *, stream: TextIO) -> str:
    if not stream.isatty():
        return text
    return f"{style}{text}\033[0m"
