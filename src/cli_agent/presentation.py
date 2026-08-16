"""Terminal presentation for provider-neutral model events."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TextIO

from rich.console import Console as _Console
from rich.live import Live as _Live
from rich.markdown import Markdown as _Markdown
from rich.theme import Theme as _Theme

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


class _MarkdownRenderer(Protocol):
    """Presentation surface required by model event rendering."""

    def feed(self, text: str) -> None:
        """Render one streamed text fragment."""

    def suspend(self) -> None:
        """Stop the active streamed display before a diagnostic."""


_REFRESH_PER_SECOND = 5
_HEIGHT_MARGIN = 4
_MARKDOWN_THEME = _Theme({"markdown.code": "bold #a0522d"})
_FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

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


class MarkdownStreamRenderer:
    """Render streamed Markdown to a TTY with rich Live."""

    def __init__(self, stdout: TextIO) -> None:
        """Create a renderer writing to `stdout`."""

        self._stdout = stdout
        self._enabled = stdout.isatty()
        no_color = "NO_COLOR" in os.environ
        self._console = _Console(
            file=stdout,
            highlight=False,
            color_system=None if no_color else "auto",
            force_terminal=False if no_color else None,
            theme=_MARKDOWN_THEME,
        )
        self._buffer: list[str] = []
        self._code_fence: str | None = None
        # Conservative upper bound of the active segment's rendered height;
        # exact measurement only happens once the bound nears the screen.
        self._height_bound = 0
        self._live: _Live | None = None

    def feed(self, text: str) -> None:
        """Append streamed Markdown and refresh the active Live display.

        Once the rendered segment reaches terminal height it is settled as
        static text and the next fragment opens a fresh Live segment below,
        so a Live region never grows taller than the screen.  Taller regions
        cannot be repainted in place (cursor-up clamps at the top row) and
        every refresh would scroll a duplicate of the document head into the
        terminal scrollback.
        """

        if not text:
            return
        if not self._enabled:
            self._stdout.write(text)
            self._stdout.flush()
            return

        self._buffer.append(text)
        live = self._live
        if live is None:
            live = self._new_live()
            self._live = live
        if not live.is_started:
            live.start()
        markdown = self._markdown()
        live.update(markdown)
        self._height_bound += (
            2 * text.count("\n") + len(text) // self._console.size.width + 2
        )
        if self._height_bound < self._console.size.height - _HEIGHT_MARGIN:
            return
        height = self._rendered_height(markdown)
        self._height_bound = height
        if height >= self._console.size.height:
            self._settle_overflow()

    def suspend(self) -> None:
        """Stop the current Live display and close its rendered text segment."""

        live = self._live
        if live is None:
            return

        live.stop()
        self._live = None
        self._code_fence = _open_code_fence(self._text())
        self._buffer.clear()
        self._height_bound = _HEIGHT_MARGIN if self._code_fence is not None else 0

    def resume(self) -> None:
        """Prepare a fresh Live display for the next streamed fragment."""

        if self._live is None:
            self._live = self._new_live()

    def finish(self) -> None:
        """Stop Live, flush pending text, and clear the renderer for reuse."""

        live = self._live
        if live is not None:
            live.stop()
            self._live = None
        elif self._enabled and self._buffer:
            pending = self._new_live()
            pending.start()
            pending.update(self._markdown())
            pending.stop()
        self._code_fence = None
        self._height_bound = 0
        self._buffer.clear()

    def _new_live(self) -> _Live:
        # Streaming frames stay within the screen because feed() settles at
        # terminal height; stop() forces "visible" for the complete final
        # frame, so the default "ellipsis" overflow never clips in practice.
        return _Live(
            console=self._console,
            auto_refresh=True,
            refresh_per_second=_REFRESH_PER_SECOND,
        )

    def _text(self) -> str:
        text = "".join(self._buffer)
        if self._code_fence is not None:
            text = f"{self._code_fence}\n{text}"
        return text

    def _markdown(self) -> _Markdown:
        return self._markdown_of("".join(self._buffer))

    def _markdown_of(self, text: str) -> _Markdown:
        if self._code_fence is not None:
            text = f"{self._code_fence}\n{text}"
        return _Markdown(text, code_theme="friendly")

    def _rendered_height(self, markdown: _Markdown) -> int:
        """Measure the terminal lines `markdown` occupies at console width."""

        lines = self._console.render_lines(
            markdown,
            self._console.options,
            pad=False,
        )
        return len(lines)

    def _settle_overflow(self) -> None:
        """Settle the active segment up to its last complete line.

        The partial line at the crossing stays buffered so the next segment
        starts at a line boundary and a split line is never garbled across
        segments.
        """

        live = self._live
        if live is None:
            return
        text = "".join(self._buffer)
        if "\n" not in text or text.endswith("\n"):
            self.suspend()
            return
        prefix, keep = text.rsplit("\n", 1)
        if _FENCE_PATTERN.match(keep):
            self.suspend()
            return
        fence = _open_code_fence(self._text())
        live.update(self._markdown_of(prefix))
        live.stop()
        self._live = None
        self._buffer[:] = [keep]
        self._code_fence = fence
        self._height_bound = 1 + (_HEIGHT_MARGIN if fence is not None else 0)


def _open_code_fence(text: str) -> str | None:
    """Return the opening fence line if `text` ends inside a fenced code block."""

    opener: str | None = None
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        match = _FENCE_PATTERN.match(line)
        if match is None:
            continue
        marks = match.group(1)
        info = match.group(2).strip()
        if opener is None:
            if marks[0] == "`" and "`" in info:
                continue
            opener = line.strip()
            fence_char = marks[0]
            fence_length = len(marks)
        elif marks[0] == fence_char and len(marks) >= fence_length and not info:
            opener = None
    return opener


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
    renderer: _MarkdownRenderer | None = None,
) -> None:
    """Render one provider-neutral event."""

    if isinstance(event, TextDelta):
        if renderer is not None:
            renderer.feed(event.text)
            return
        stdout.write(event.text)
        stdout.flush()
        return

    if isinstance(event, ToolCallReady):
        if renderer is not None:
            renderer.suspend()
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
        if renderer is not None:
            renderer.suspend()
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
