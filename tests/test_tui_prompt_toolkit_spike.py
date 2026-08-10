"""Spike tests for the prompt_toolkit TTY integration.

These tests intentionally exercise prompt_toolkit directly.  The project
adapter belongs to the next TUI issue; this file records the smallest verified
integration surface that adapter can rely on.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import select
import signal
import sys
import termios
import time
from io import StringIO
from typing import Any

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_input, create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput, create_output

CTRL_J = "\n"
_PROMPT = "spike> "
_PROMPT_MARKER = b"spike>"
_PTY_TIMEOUT = 5.0


def _key_bindings(*, raise_on_exclamation: bool = False) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add(Keys.ControlJ)
    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add(Keys.ControlM)
    def _accept_input(event) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add(Keys.ControlC)
    def _clear_or_interrupt(event) -> None:
        if event.current_buffer.text:
            event.current_buffer.reset()
            event.app.invalidate()
        else:
            event.app.exit(exception=KeyboardInterrupt())

    @bindings.add(Keys.ControlD)
    def _eof_on_empty_input(event) -> None:
        if not event.current_buffer.text:
            event.app.exit(exception=EOFError())

    if raise_on_exclamation:

        @bindings.add("!")
        def _raise_unexpected_error(event) -> None:
            event.app.exit(exception=RuntimeError("spike failure"))

    return bindings


async def _prompt_from_pipe(
    data: str,
    *,
    output: Any | None = None,
    raise_on_exclamation: bool = False,
) -> str:
    with create_pipe_input() as input_stream:
        prompt_output = output or DummyOutput()
        session = PromptSession(
            input=input_stream,
            output=prompt_output,
            key_bindings=_key_bindings(
                raise_on_exclamation=raise_on_exclamation,
            ),
            multiline=True,
        )
        input_stream.send_text(data)
        return await asyncio.wait_for(
            session.prompt_async(_PROMPT, handle_sigint=False),
            timeout=1,
        )


def test_prompt_async_does_not_block_the_event_loop() -> None:
    async def scenario() -> tuple[str, int]:
        with create_pipe_input() as input_stream:
            session = PromptSession(
                input=input_stream,
                output=DummyOutput(),
                key_bindings=_key_bindings(),
                multiline=True,
            )
            prompt_task = asyncio.create_task(
                session.prompt_async(_PROMPT, handle_sigint=False)
            )

            ticks = 0
            for _ in range(3):
                await asyncio.sleep(0.01)
                ticks += 1

            assert not prompt_task.done()
            input_stream.send_text("ready\r")
            return await prompt_task, ticks

    result, ticks = asyncio.run(scenario())

    assert result == "ready"
    assert ticks == 3


def test_ctrl_j_is_distinguishable_from_enter() -> None:
    result = asyncio.run(_prompt_from_pipe(f"first{CTRL_J}second\r"))

    assert result == "first\nsecond"


def test_tty_output_can_use_stderr_without_contaminating_stdout() -> None:
    stderr = StringIO()
    stdout = StringIO()

    result = asyncio.run(
        _prompt_from_pipe(
            "task\r",
            output=create_output(
                stdout=stderr,
            ),
        )
    )
    stdout.write("model body")

    assert result == "task"
    assert "spike> " in stderr.getvalue()
    assert stdout.getvalue() == "model body"


def test_first_ctrl_c_clears_non_empty_input() -> None:
    result = asyncio.run(_prompt_from_pipe("draft\x03after\r"))

    assert result == "after"


def test_ctrl_c_on_empty_input_raises_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(_prompt_from_pipe("\x03"))


def test_ctrl_d_on_empty_input_raises_eof() -> None:
    with pytest.raises(EOFError):
        asyncio.run(_prompt_from_pipe("\x04"))


@pytest.mark.skipif(os.name != "posix", reason="the spike targets POSIX PTYs")
@pytest.mark.parametrize(
    ("input_data", "expected_result", "expected_error"),
    (
        (b"task\r", "task", None),
        (f"first{CTRL_J}second\r".encode(), "first\nsecond", None),
        (b"\x04", None, "EOFError"),
        (b"\x03", None, "KeyboardInterrupt"),
        (b"!", None, "RuntimeError"),
    ),
)
def test_pty_prompt_restores_terminal_state(
    input_data: bytes,
    expected_result: str | None,
    expected_error: str | None,
) -> None:
    result, output, before, after = _run_pty_prompt(input_data)

    assert result == expected_result
    assert result is not None or output["error"] == expected_error
    assert _without_pendin(after) == _without_pendin(before)
    assert "spike>" in output["terminal_output"]
    assert "\x1b[?2004l" in output["terminal_output"]
    assert "\x1b[?25h" in output["terminal_output"]


def _run_pty_prompt(
    input_data: bytes,
) -> tuple[str | None, dict[str, Any], list[Any], list[Any]]:
    master_fd, slave_fd = os.openpty()
    report_read, report_write = os.pipe()
    before = termios.tcgetattr(slave_fd)

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.close(report_read)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 2)
        os.close(slave_fd)

        sys.stdin = os.fdopen(
            0,
            "r",
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

        result: dict[str, Any]
        try:
            result = asyncio.run(_run_child_prompt())
        except BaseException as exc:
            result = {"error": type(exc).__name__}
        try:
            os.write(report_write, json.dumps(result).encode("utf-8"))
        finally:
            os.close(report_write)
            os._exit(0)

    terminal_output = bytearray()
    child_reaped = False
    try:
        _read_until(master_fd, _PROMPT_MARKER, terminal_output)
        os.write(master_fd, input_data)
        status = _wait_for_child(pid, master_fd, terminal_output)
        child_reaped = True
        _drain_pty(master_fd, terminal_output)
        report = os.read(report_read, 65536)
        if not report:
            raise AssertionError(f"PTY child exited without a report: {status}")
        output = json.loads(report.decode("utf-8"))
        output["terminal_output"] = terminal_output.decode(
            "utf-8",
            errors="replace",
        )
        after = termios.tcgetattr(slave_fd)
        return output.get("result"), output, before, after
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
        os.close(report_read)
        os.close(master_fd)
        os.close(slave_fd)


def _without_pendin(attrs: list[Any]) -> list[Any]:
    normalized = list(attrs)
    pendin = getattr(termios, "PENDIN", 0)
    normalized[3] &= ~pendin
    return normalized


async def _run_child_prompt() -> dict[str, Any]:
    input_stream = create_input(sys.stdin, always_prefer_tty=True)
    output_stream = create_output(sys.stderr, always_prefer_tty=True)
    session = PromptSession(
        input=input_stream,
        output=output_stream,
        key_bindings=_key_bindings(raise_on_exclamation=True),
        multiline=True,
    )
    result = await session.prompt_async(_PROMPT, handle_sigint=False)
    return {"result": result}


def _read_until(fd: int, needle: bytes, buffer: bytearray) -> None:
    deadline = time.monotonic() + _PTY_TIMEOUT
    cpr_responded = False
    while needle not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {needle!r}; output={buffer!r}")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        buffer.extend(chunk)
        if not cpr_responded and b"\x1b[6n" in buffer:
            os.write(fd, b"\x1b[1;1R")
            cpr_responded = True


def _wait_for_child(
    pid: int,
    master_fd: int,
    terminal_output: bytearray,
) -> tuple[int, int]:
    deadline = time.monotonic() + _PTY_TIMEOUT
    while True:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
        if waited_pid:
            return waited_pid, status

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise AssertionError("timed out waiting for PTY prompt to exit")

        ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.05))
        if not ready:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                continue
            raise
        if chunk:
            terminal_output.extend(chunk)


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
