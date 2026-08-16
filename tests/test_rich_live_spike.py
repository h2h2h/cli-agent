"""Spike tests for the rich Live streaming lifecycle.

These tests intentionally exercise rich directly.  The project adapter
belongs to the next presentation issue; this file records the smallest
verified integration surface that adapter can rely on:

- ``Live.start()/stop()`` cycles flush static content and keep prior
  content visible (StringIO verification).
- A *fresh* ``Live`` instance per text segment (created after a diagnostic
  print) renders below the diagnostic without cursor-up erases, so the
  diagnostic survives subsequent refreshes.  Reusing one ``Live`` across
  ``stop()``/``start()`` with a diagnostic printed in between clobbers the
  diagnostic when the new segment is shorter than the old one (stale shape).
- ``stop()`` forces ``vertical_overflow="visible"``, so the final frame is
  always complete regardless of the streaming overflow mode.
- ``Markdown`` on a non-terminal console strips the source syntax, so non-TTY
  passthrough must keep raw text (never route it through ``Markdown``).
- rich 15 ``NO_COLOR`` strips colors but keeps style attributes (bold SGR
  still emitted); ``color_system=None`` suppresses all ANSI.
"""

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
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

_PTY_TIMEOUT = 5.0
_WINSIZE = struct.pack("HHHH", 8, 60, 0, 0)


def _terminal_console(**kwargs: Any) -> Console:
    return Console(
        file=StringIO(),
        force_terminal=True,
        width=60,
        **kwargs,
    )


def test_start_stop_cycles_flush_static_content() -> None:
    console = _terminal_console()
    live = Live(console=console, auto_refresh=False, vertical_overflow="visible")

    live.start()
    live.update(Markdown("**first**"))
    live.refresh()
    live.stop()

    live.start()
    live.update(Markdown("second"))
    live.refresh()
    live.stop()

    value = console.file.getvalue()
    assert "first" in value
    assert "second" in value
    assert value.index("first") < value.index("second")


def test_start_is_idempotent_and_stop_without_start_is_noop() -> None:
    console = _terminal_console()
    live = Live(console=console, auto_refresh=False, vertical_overflow="visible")

    live.start()
    live.start()
    assert live.is_started

    live.stop()
    live.stop()
    assert not live.is_started
    assert "first" not in console.file.getvalue()
    assert "second" not in console.file.getvalue()


def test_stop_forces_visible_overflow_and_complete_final_frame() -> None:
    console = _terminal_console()
    live = Live(console=console, auto_refresh=False, vertical_overflow="ellipsis")

    live.start()
    long = "\n".join(f"line {i}" for i in range(30))
    live.update(Markdown(long))
    live.refresh()
    live.stop()

    assert live.vertical_overflow == "visible"
    value = console.file.getvalue()
    assert value.count("line ") >= 30


def test_markdown_renders_bold_and_table() -> None:
    console = _terminal_console()
    console.print(Markdown("**bold**\n\n| a | b |\n|---|---|\n| 1 | 2 |"))

    value = console.file.getvalue()
    assert "\x1b[1m" in value
    assert "bold" in value
    assert all(marker in value for marker in ("a", "b", "1", "2"))


def test_non_terminal_console_strips_markdown_source() -> None:
    out = StringIO()
    console = Console(file=out, width=60)
    console.print(Markdown("**bold** and `code`"))

    value = out.getvalue()
    assert "**bold**" not in value
    assert "bold" in value
    assert "\x1b[" not in value


def test_no_color_keeps_bold_sgr_but_color_system_none_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    console = _terminal_console()
    console.print(Markdown("**bold**"))
    assert "\x1b[1m" in console.file.getvalue()

    console_clean = _terminal_console(color_system=None)
    console_clean.print(Markdown("**bold**"))
    assert "\x1b[" not in console_clean.file.getvalue()


@pytest.mark.skipif(os.name != "posix", reason="the spike targets POSIX PTYs")
def test_pty_fresh_live_segments_preserve_diagnostics_in_order() -> None:
    text, before, after = _run_pty_child(_fresh_live_segments_child)

    indices = [text.index(marker) for marker in (
        "first-seg",
        "DIAG-1",
        "tail-seg",
        "DIAG-2",
        "grow-seg",
        "DONE",
    )]
    assert indices == sorted(indices)
    assert "\x1b[?25h" in text

    between = text.split("DIAG-1", 1)[1].split("tail-seg", 1)[0]
    assert "\x1b[1A" not in between
    assert _without_pendin(after) == _without_pendin(before)


@pytest.mark.skipif(os.name != "posix", reason="the spike targets POSIX PTYs")
def test_pty_reused_live_clobbers_diagnostic_on_shrink() -> None:
    text, before, after = _run_pty_child(_reused_live_child)

    between = text.split("DIAG-1", 1)[1].split("tail", 1)[0]
    assert "\x1b[1A" in between
    assert _without_pendin(after) == _without_pendin(before)


@pytest.mark.skipif(os.name != "posix", reason="the spike targets POSIX PTYs")
def test_pty_overflow_modes_end_with_complete_content() -> None:
    text, before, after = _run_pty_child(_overflow_modes_child)

    assert all(f"L{i:02d}" in text for i in range(30))
    assert "..." in text
    assert text.index("...") < text.index("DONE")
    assert _without_pendin(after) == _without_pendin(before)


def _fresh_live_segments_child() -> None:
    console = Console(file=sys.stdout)

    def new_live() -> Live:
        return Live(
            console=console,
            auto_refresh=True,
            refresh_per_second=5,
            vertical_overflow="visible",
        )

    live = new_live()
    live.start()
    live.update(Markdown("first-seg\n\nline2\n\nline3"))
    time.sleep(0.3)
    live.stop()

    print("DIAG-1", file=sys.stderr, flush=True)

    live = new_live()
    live.start()
    live.update(Markdown("tail-seg"))
    time.sleep(0.3)
    live.stop()

    print("DIAG-2", file=sys.stderr, flush=True)

    live = new_live()
    live.start()
    live.update(Markdown("grow-seg\n\nb\n\nc"))
    time.sleep(0.3)
    live.stop()

    print("DONE", file=sys.stderr, flush=True)


def _reused_live_child() -> None:
    console = Console(file=sys.stdout)
    live = Live(
        console=console,
        auto_refresh=True,
        refresh_per_second=5,
        vertical_overflow="visible",
    )

    live.start()
    live.update(Markdown("first-seg\n\nline2\n\nline3"))
    time.sleep(0.3)
    live.stop()

    print("DIAG-1", file=sys.stderr, flush=True)

    live.start()
    live.update(Markdown("tail-seg"))
    time.sleep(0.3)
    live.stop()

    print("DONE", file=sys.stderr, flush=True)


def _overflow_modes_child() -> None:
    console = Console(file=sys.stdout)
    lines = "\n".join(f"- L{i:02d}" for i in range(30))

    for mode in ("visible", "ellipsis"):
        live = Live(
            console=console,
            auto_refresh=True,
            refresh_per_second=5,
            vertical_overflow=mode,
        )
        live.start()
        live.update(Markdown(lines))
        time.sleep(0.3)
        live.stop()

    print("DONE", file=sys.stderr, flush=True)


def _run_pty_child(child: Callable[[], None]) -> tuple[str, list[Any], list[Any]]:
    master_fd, slave_fd = os.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, _WINSIZE)
    before = termios.tcgetattr(slave_fd)

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(slave_fd)
        sys.stdout = os.fdopen(
            1,
            "w",
            encoding="utf-8",
            buffering=1,
            closefd=False,
        )
        sys.stderr = os.fdopen(
            2,
            "w",
            encoding="utf-8",
            buffering=1,
            closefd=False,
        )
        try:
            child()
        except BaseException:
            import traceback

            traceback.print_exc()
        os._exit(0)

    output = bytearray()
    child_reaped = False
    try:
        deadline = time.monotonic() + _PTY_TIMEOUT
        while b"DONE" not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out waiting for DONE; output={output!r}"
                )
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

        _, _ = os.waitpid(pid, 0)
        child_reaped = True
        _drain_pty(master_fd, output)
        after = termios.tcgetattr(slave_fd)
        return output.decode("utf-8", errors="replace"), before, after
    finally:
        if not child_reaped:
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


def _without_pendin(attrs: list[Any]) -> list[Any]:
    normalized = list(attrs)
    pendin = getattr(termios, "PENDIN", 0)
    normalized[3] &= ~pendin
    return normalized
