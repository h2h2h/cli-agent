"""Unit tests for the TTY Markdown presentation adapter."""

from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import struct
import sys
import termios
import time
import traceback
from collections.abc import Callable
from io import StringIO

import pytest

from cli_agent.presentation import (
    MarkdownStreamRenderer,
    _open_code_fence,
    render_event,
)
from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    TextDelta,
    ToolCall,
    ToolCallReady,
)

_PTY_TIMEOUT = 8.0
_WINSIZE = struct.pack("HHHH", 8, 60, 0, 0)


class _TerminalOutput(StringIO):
    def isatty(self) -> bool:
        return True


def test_renderer_renders_markdown_styles_and_table() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    renderer.feed("# Title\n\n**bold** and `code`\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    renderer.finish()

    value = output.getvalue()
    assert "\033[1m" in value
    assert "bold" in value
    assert "code" in value
    assert all(marker in value for marker in ("Title", "a", "b", "1", "2"))
    assert "| a | b |" not in value


def test_inline_code_uses_a_readable_style_without_background() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    renderer.feed("`code`")
    renderer.finish()

    value = output.getvalue()
    assert "\033[1;38;" in value
    assert "\033[1;33;40m" not in value
    assert "\033[40m" not in value


def test_fragmented_feed_matches_single_feed() -> None:
    fragmented_output = _TerminalOutput()
    fragmented = MarkdownStreamRenderer(fragmented_output)
    fragmented.feed("**bo")
    fragmented.feed("ld** and ")
    fragmented.feed("`code`")
    fragmented.finish()

    complete_output = _TerminalOutput()
    complete = MarkdownStreamRenderer(complete_output)
    complete.feed("**bold** and `code`")
    complete.finish()

    assert fragmented_output.getvalue() == complete_output.getvalue()


def test_non_terminal_feed_is_raw_passthrough() -> None:
    output = StringIO()
    renderer = MarkdownStreamRenderer(output)

    renderer.feed("**bold**")
    renderer.finish()

    assert output.getvalue() == "**bold**"
    assert renderer._live is None


def test_suspend_uses_a_fresh_live_and_finish_clears_state() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    renderer.feed("first")
    first_live = renderer._live
    renderer.suspend()
    renderer.resume()
    second_live = renderer._live
    renderer.feed(" second")
    renderer.finish()

    assert first_live is not None
    assert second_live is not None
    assert second_live is not first_live
    assert renderer._live is None
    assert renderer._buffer == []
    assert "first" in output.getvalue()
    assert "second" in output.getvalue()
    assert output.getvalue().count("first") == 1


def test_feed_below_terminal_height_stays_live() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    renderer.feed("small")
    assert renderer._live is not None
    assert renderer._live.is_started
    assert renderer._buffer == ["small"]

    renderer.finish()
    assert renderer._live is None


def test_feed_settles_segment_at_terminal_height() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    lines = [f"row {i:02d}" for i in range(30)]
    renderer.feed("```\n" + "\n".join(lines) + "\n```")

    assert renderer._live is None
    assert renderer._buffer == []

    renderer.finish()
    value = output.getvalue()
    assert all(line in value for line in lines)
    assert value.count("row 05") == 1


def test_settle_keeps_partial_line_and_finish_flushes_it() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    lines = [f"row {i:02d}" for i in range(30)]
    renderer.feed("```\n" + "\n".join(lines))

    assert renderer._live is None
    assert "".join(renderer._buffer) == "row 29"

    renderer.finish()
    value = output.getvalue()
    assert all(line in value for line in lines)
    assert value.count("row 29") == 1


def test_settle_inside_code_fence_reopens_it_in_the_next_segment() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    code_lines = "\n".join(f"row {i:02d}" for i in range(30))
    renderer.feed(f"```python\n{code_lines}")
    assert renderer._code_fence == "```python"

    renderer.feed("\nlast_row\n```\n\nafter")
    renderer.suspend()
    assert renderer._code_fence is None

    value = output.getvalue()
    assert "last_row" in value
    assert "after" in value
    assert "```" not in value
    renderer.finish()


def test_open_code_fence_tracks_commonmark_rules() -> None:
    assert _open_code_fence("```python\nx = 1") == "```python"
    assert _open_code_fence("```python\nx = 1\n```") is None
    assert _open_code_fence("~~~\nx") == "~~~"
    assert _open_code_fence("````\n```\nx") == "````"
    assert _open_code_fence("```\n```\n```") == "```"
    assert _open_code_fence("text only") is None


@pytest.mark.skipif(os.name != "posix", reason="PTY rendering requires POSIX")
def test_pty_tall_stream_settles_without_scrollback_duplication() -> None:
    text = _run_pty_child(_tall_stream_child)

    assert all(f"line_{i:02d}" in text for i in range(30))
    assert "end of doc" in text
    assert "\x1b[1A\x1b[2K" * 8 not in text
    assert len(text) < 30000


def test_render_event_feeds_text_and_suspends_before_diagnostics() -> None:
    class FakeRenderer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def feed(self, text: str) -> None:
            self.calls.append(("feed", text))

        def suspend(self) -> None:
            self.calls.append(("suspend", None))

    renderer = FakeRenderer()
    stdout = StringIO()
    stderr = StringIO()
    call = ToolCall(call_id="call-1", name="output", arguments={})

    render_event(
        TextDelta(text="answer"),
        stdout=stdout,
        stderr=stderr,
        renderer=renderer,
    )
    render_event(
        ToolCallReady(call=call),
        stdout=stdout,
        stderr=stderr,
        renderer=renderer,
    )
    render_event(
        ModelCompletion(
            message=AssistantMessage.text("answer"),
            finish_reason="stop",
        ),
        stdout=stdout,
        stderr=stderr,
        renderer=renderer,
    )

    assert renderer.calls == [
        ("feed", "answer"),
        ("suspend", None),
        ("suspend", None),
    ]
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "[tool] output\n[completion] reason=stop\n"


def test_no_color_disables_all_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    renderer.feed("**bold**")
    renderer.finish()

    assert "bold" in output.getvalue()
    assert "\033[" not in output.getvalue()


def _tall_stream_child() -> None:
    renderer = MarkdownStreamRenderer(sys.stdout)
    code = "\n".join(f"line_{i:02d} = {i}" for i in range(30))
    chunks = (
        "# Title\n\nTOP-MARKER\n\n",
        "```python\n",
        code[:150],
        code[150:],
        "\n```\n\nend of doc\n",
    )
    for chunk in chunks:
        renderer.feed(chunk)
        time.sleep(0.3)
    renderer.finish()
    print("DONE", file=sys.stderr, flush=True)


def _run_pty_child(child: Callable[[], None]) -> str:
    master_fd, slave_fd = os.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, _WINSIZE)

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(slave_fd)
        sys.stdout = os.fdopen(1, "w", encoding="utf-8", buffering=1, closefd=False)
        sys.stderr = os.fdopen(2, "w", encoding="utf-8", buffering=1, closefd=False)
        try:
            child()
        except BaseException:
            traceback.print_exc()
        os._exit(0)

    output = bytearray()
    reaped = False
    try:
        deadline = time.monotonic() + _PTY_TIMEOUT
        while b"DONE" not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for DONE; output={output!r}")
            ready, _, _ = select.select([master_fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)

        os.waitpid(pid, 0)
        reaped = True
        _drain_pty(master_fd, output)
        return output.decode("utf-8", errors="replace")
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        os.close(master_fd)
        os.close(slave_fd)


def _drain_pty(fd: int, buffer: bytearray) -> None:
    while True:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            return
        try:
            chunk = os.read(fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise
        if not chunk:
            return
        buffer.extend(chunk)
