"""POSIX PTY integration tests for the CLI-owned TUI session."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import select
import signal
import sys
import termios
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from policy_fakes import _AskExecutablePolicy

from cli_agent import cli as cli_module
from cli_agent.config import CliConfig
from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UserMessage,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="the CLI TUI is supported and tested on POSIX PTYs",
)

_COMPLETION_STOP = b"reason=stop"
_INTERACTION_PROMPT = b"Allow once? [y/N]"
_PROMPT = b"cli-agent>"
_SLASH_DESCRIPTION = b"End the current interactive session"
_PTY_TIMEOUT = 5.0


def _assert_cli_pty_input_variant(
    tmp_path: Path,
    input_data: bytes,
    expected_task: str,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="single",
        steps=(
            (input_data, _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == [expected_task]
    assert result.stdout == "completed\n"


@pytest.mark.parametrize(
    ("input_data", "expected_task"),
    (
        (b"first\nsecond\r", "first\nsecond"),
        (b"ac\x1b[Db\r", "abc"),
        (b"abcd\x1b[D\x1b[D\x1b[3~\r", "abd"),
        (
            b"\x1b[200~line one\nline two\x1b[201~\r",
            "line one\nline two",
        ),
    ),
)
def test_cli_pty_input_variants(
    tmp_path: Path,
    input_data: bytes,
    expected_task: str,
) -> None:
    _assert_cli_pty_input_variant(
        tmp_path,
        input_data,
        expected_task,
    )


def test_cli_pty_history_supports_backward_and_forward_navigation(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="history",
        steps=(
            (b"first\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b"second\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b"\x10\x10\x0e\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == ["first", "second", "second"]
    assert result.stdout == "completed\n" * 3


def test_cli_pty_first_ctrl_c_clears_non_empty_input(tmp_path: Path) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="single",
        steps=(
            (b"draft\x03after\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == ["after"]


@pytest.mark.parametrize(
    ("input_data", "expected_exit_code"),
    ((b"\x04", 0), (b"\x03", 130)),
)
def test_cli_pty_empty_eof_and_ctrl_c_restore_terminal(
    tmp_path: Path,
    input_data: bytes,
    expected_exit_code: int,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="empty",
        steps=((input_data, None),),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == expected_exit_code
    assert result.report["tasks"] == []
    if expected_exit_code == 130:
        assert "cli-agent: interrupted" in result.terminal_output


def test_cli_pty_runtime_exception_restores_terminal(tmp_path: Path) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="failure",
        steps=((b"task\r", None),),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 1
    assert result.report["tasks"] == ["task"]
    assert "[error] code=internal_error An internal Runtime error" in (
        result.terminal_output
    )
    assert "ScriptedModelProvider received more model requests" not in (
        result.terminal_output
    )


def test_cli_pty_markdown_renders_in_one_shot_and_interactive_modes(
    tmp_path: Path,
) -> None:
    one_shot_workspace = tmp_path / "one-shot"
    interactive_workspace = tmp_path / "interactive"
    one_shot_workspace.mkdir()
    interactive_workspace.mkdir()
    one_shot = _run_cli_pty(
        one_shot_workspace,
        scenario="markdown-one-shot",
        stdout_is_tty=True,
        initial_marker=None,
        steps=(),
    )
    interactive = _run_cli_pty(
        interactive_workspace,
        scenario="markdown",
        stdout_is_tty=True,
        steps=(
            (b"render markdown\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_renderer_restored(one_shot)
    _assert_restored(interactive)
    assert one_shot.report["exit_code"] == 0
    assert interactive.report["exit_code"] == 0
    for result in (one_shot, interactive):
        _assert_markdown_content(result.terminal_output)
        assert "**bold**" not in result.terminal_output
        assert "| key | value |" not in result.terminal_output
    assert _markdown_content_signature(one_shot.terminal_output) == (
        _markdown_content_signature(interactive.terminal_output)
    )


def test_cli_pty_markdown_keeps_tool_diagnostic_between_segments(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="markdown-tool",
        stdout_is_tty=True,
        steps=(
            (b"render markdown\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    text = result.terminal_output
    assert text.index("Before") < text.index("[tool] exec") < text.index("After")
    assert "\033[1m" in text
    assert "**bold**" not in text


def test_cli_pty_markdown_no_color_removes_markdown_ansi(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="markdown-no-color",
        stdout_is_tty=True,
        initial_marker=None,
        steps=(),
    )

    assert _without_pendin(result.after) == _without_pendin(result.before)
    assert result.report["exit_code"] == 0
    assert "bold" in result.terminal_output
    assert "\033[1m" not in result.terminal_output
    assert "**bold**" not in result.terminal_output


def test_cli_pty_markdown_interrupt_restores_terminal(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="markdown-interrupt",
        stdout_is_tty=True,
        steps=((b"render markdown\r", b"Partial"), (b"\x03", None)),
    )

    _assert_renderer_restored(result)
    assert result.report["exit_code"] == 130
    assert result.report["tasks"] == ["render markdown"]
    assert "cli-agent: interrupted" in result.terminal_output


@pytest.mark.parametrize(
    ("answer", "expected_exists"),
    ((b"y\r", False), (b"maybe\r", True)),
)
def test_cli_pty_permission_confirmation_accepts_only_yes(
    tmp_path: Path,
    answer: bytes,
    expected_exists: bool,
) -> None:
    proof = tmp_path / "approval-proof.txt"
    proof.write_text("preserved", encoding="utf-8")

    result = _run_cli_pty(
        tmp_path,
        scenario="permission",
        steps=(
            (b"rm approval-proof.txt\r", _INTERACTION_PROMPT),
            (answer, _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == ["rm approval-proof.txt"]
    assert proof.exists() is expected_exists
    assert result.stdout == "completed\n"


def test_cli_pty_slash_shows_candidate_and_description_on_stderr(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="empty",
        steps=(
            (b"/", _SLASH_DESCRIPTION),
            (b"\x03", None),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == []
    assert b"/exit" in result.terminal_output.encode()
    assert _SLASH_DESCRIPTION in result.terminal_output.encode()
    assert "cli-agent>" not in result.stdout
    assert "/exit" not in result.stdout


def test_cli_pty_slash_prefix_filter_and_reopen_after_delete(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="empty",
        steps=(
            (b"/e", _SLASH_DESCRIPTION),
            (b"z", b"z"),
            (b"\x7f", _SLASH_DESCRIPTION),
            (b"\x03", None),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == []
    assert result.terminal_output.encode().count(_SLASH_DESCRIPTION) >= 2


def test_cli_pty_slash_tab_accepts_candidate_and_keeps_editing(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="single",
        steps=(
            (b"/e", _SLASH_DESCRIPTION),
            (b"\t now\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == ["/exit now"]
    assert result.stdout == "completed\n"


def test_cli_pty_slash_down_selects_candidate_before_tab(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="single",
        steps=(
            (b"/e", _SLASH_DESCRIPTION),
            (b"\x1b[B\t now\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == ["/exit now"]
    assert result.stdout == "completed\n"


def test_cli_pty_slash_enter_submits_buffer_without_candidate(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="single",
        steps=(
            (b"/e", _SLASH_DESCRIPTION),
            (b"\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == ["/e"]
    assert result.stdout == "completed\n"


def test_cli_pty_full_slash_exit_ends_session_without_turn(
    tmp_path: Path,
) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="empty",
        steps=((b"/exit\r", None),),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == []
    assert result.stdout == ""
    assert "[session]" in result.terminal_output


def test_cli_pty_unknown_slash_input_runs_agent_turn(tmp_path: Path) -> None:
    result = _run_cli_pty(
        tmp_path,
        scenario="single",
        steps=(
            (b"/unknown\r", _COMPLETION_STOP),
            (b"", _PROMPT),
            (b":q\r", None),
        ),
    )

    _assert_restored(result)
    assert result.report["exit_code"] == 0
    assert result.report["tasks"] == ["/unknown"]
    assert result.stdout == "completed\n"


class _PtyResult:
    def __init__(
        self,
        *,
        report: dict[str, Any],
        stdout: str,
        terminal_output: str,
        before: list[Any],
        after: list[Any],
    ) -> None:
        self.report = report
        self.stdout = stdout
        self.terminal_output = terminal_output
        self.before = before
        self.after = after


def _run_cli_pty(
    workspace: Path,
    *,
    scenario: str,
    steps: Sequence[tuple[bytes, bytes | None]],
    stdout_is_tty: bool = False,
    initial_marker: bytes | None = _PROMPT,
) -> _PtyResult:
    master_fd, slave_fd = os.openpty()
    stdout_read, stdout_write = os.pipe()
    report_read, report_write = os.pipe()
    before = termios.tcgetattr(slave_fd)
    pid = os.fork()

    if pid == 0:
        os.close(master_fd)
        os.close(stdout_read)
        os.close(report_read)
        import fcntl

        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        _run_cli_child(
            slave_fd=slave_fd,
            stdout_write=stdout_write,
            report_write=report_write,
            workspace=workspace,
            scenario=scenario,
            stdout_is_tty=stdout_is_tty,
        )

    os.close(stdout_write)
    os.close(report_write)
    terminal_output = bytearray()
    child_reaped = False
    try:
        offset = 0
        if initial_marker is not None:
            offset = _read_until(master_fd, initial_marker, terminal_output)
        for input_data, marker in steps:
            os.write(master_fd, input_data)
            if marker is not None:
                offset = _read_until(
                    master_fd,
                    marker,
                    terminal_output,
                    start=offset,
                )

        status = _wait_for_child(pid, master_fd, terminal_output)
        child_reaped = True
        _drain_pty(master_fd, terminal_output)
        report_data = _read_all(report_read)
        stdout_data = _read_all(stdout_read)
        if not report_data:
            raise AssertionError(
                f"PTY child exited without a report: {status}; "
                f"output={terminal_output!r}"
            )
        report = json.loads(report_data.decode("utf-8"))
        after = _deserialize_termios_attrs(report["terminal_attrs"])
        return _PtyResult(
            report=report,
            stdout=stdout_data.decode("utf-8", errors="replace"),
            terminal_output=terminal_output.decode("utf-8", errors="replace"),
            before=before,
            after=after,
        )
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
        os.close(stdout_read)
        os.close(master_fd)
        os.close(slave_fd)


def _run_cli_child(
    *,
    slave_fd: int,
    stdout_write: int,
    report_write: int,
    workspace: Path,
    scenario: str,
    stdout_is_tty: bool,
) -> None:
    os.dup2(slave_fd, 0)
    os.dup2(slave_fd if stdout_is_tty else stdout_write, 1)
    os.dup2(slave_fd, 2)
    for fd in (slave_fd, stdout_write):
        if fd > 2:
            os.close(fd)

    sys.stdin = os.fdopen(0, "r", encoding="utf-8", buffering=1, closefd=False)
    sys.stdout = os.fdopen(1, "w", encoding="utf-8", buffering=1, closefd=False)
    sys.stderr = os.fdopen(2, "w", encoding="utf-8", buffering=1, closefd=False)

    try:
        provider, execution_policy = _scenario(workspace, scenario)
        task = (
            "render markdown"
            if scenario in {"markdown-one-shot", "markdown-no-color"}
            else None
        )
        config = _config(workspace, task=task)
        if scenario == "markdown-no-color":
            os.environ["NO_COLOR"] = "1"
        cli_module.parse_cli_config = lambda argv=None: config
        cli_module.build_provider = lambda current_config: provider
        run_agent = cli_module.run_agent

        async def run_agent_with_policy(
            current_config: CliConfig,
            current_provider: ScriptedModelProvider,
            **kwargs: Any,
        ) -> int:
            return await run_agent(
                current_config,
                current_provider,
                execution_policy=execution_policy,
                **kwargs,
            )

        cli_module.run_agent = run_agent_with_policy
        exit_code = cli_module.main([])
        report = {
            "exit_code": exit_code,
            "tasks": _provider_tasks(provider),
        }
    except BaseException as exc:
        report = {
            "child_error": type(exc).__name__,
            "message": str(exc),
        }

    sys.stdout.flush()
    sys.stderr.flush()
    report["terminal_attrs"] = _serialize_termios_attrs(termios.tcgetattr(0))
    _write_json(report_write, report)
    os.close(report_write)
    os._exit(0)


def _scenario(
    workspace: Path,
    name: str,
) -> tuple[ScriptedModelProvider, object | None]:
    if name == "single":
        return _response_provider(1), None
    if name == "history":
        return _response_provider(3), None
    if name == "empty" or name == "failure":
        return ScriptedModelProvider(script=()), None
    if name == "permission":
        call = ToolCall(
            call_id="pty_rm",
            name="exec",
            arguments={"command": "rm approval-proof.txt"},
        )
        return (
            ScriptedModelProvider(
                script=(
                    (
                        ToolCallReady(call=call),
                        ModelCompletion(
                            message=AssistantMessage(content=(call,)),
                            finish_reason="tool_calls",
                        ),
                    ),
                    _completion_events(),
                )
            ),
            _AskExecutablePolicy(
                frozenset({"rm"}),
                rule_id="test.ask-rm-pty",
                reason="direct invocation of 'rm' requires Host approval",
            ),
        )
    if name in {"markdown", "markdown-one-shot", "markdown-no-color"}:
        return _markdown_provider(), None
    if name == "markdown-tool":
        return _markdown_tool_provider(), None
    if name == "markdown-interrupt":
        return _InterruptingProvider(), None
    raise AssertionError(f"unknown PTY scenario: {name}")


def _response_provider(count: int) -> ScriptedModelProvider:
    return ScriptedModelProvider(
        script=tuple(_completion_events() for _ in range(count)),
    )


def _completion_events() -> tuple[TextDelta | ModelCompletion, ...]:
    return (
        TextDelta(text="completed"),
        ModelCompletion(
            message=AssistantMessage.text("completed"),
            finish_reason="stop",
        ),
    )


def _markdown_provider() -> ScriptedModelProvider:
    text = (
        TextDelta(text="Intro **bold**.\n\n"),
        TextDelta(text="| key | value |\n|---|---|\n| one | two |\n\n"),
        TextDelta(text="Final `code`."),
    )
    return ScriptedModelProvider(
        script=(
            (
                *text,
                ModelCompletion(
                    message=AssistantMessage.text("Intro **bold**."),
                    finish_reason="stop",
                ),
            ),
        )
    )


def _markdown_tool_provider() -> ScriptedModelProvider:
    call = ToolCall(
        call_id="markdown_tool",
        name="exec",
        arguments={"command": "printf tool-result"},
    )
    return ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Before **bold**.\n\n"),
                ToolCallReady(call=call),
                ModelCompletion(
                    message=AssistantMessage(content=(call,)),
                    finish_reason="tool_calls",
                ),
            ),
            (
                TextDelta(text="After **tool**."),
                ModelCompletion(
                    message=AssistantMessage.text("After **tool**."),
                    finish_reason="stop",
                ),
            ),
        )
    )


class _InterruptingProvider:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def generate(self, request: object) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        yield TextDelta(text="Partial **answer**")
        await asyncio.Event().wait()


def _provider_tasks(provider: ScriptedModelProvider) -> list[str]:
    tasks: list[str] = []
    user_count = 0
    for request in provider.requests:
        user_messages = tuple(
            message for message in request.messages if isinstance(message, UserMessage)
        )
        if len(user_messages) > user_count:
            tasks.extend(
                message.content[0].text for message in user_messages[user_count:]
            )
            user_count = len(user_messages)
    return tasks


def _config(workspace: Path, *, task: str | None = None) -> CliConfig:
    return CliConfig(
        task=task,
        workspace=workspace,
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=4_096,
    )


def _assert_restored(result: _PtyResult) -> None:
    assert _without_pendin(result.after) == _without_pendin(result.before)
    assert "\x1b[?2004l" in result.terminal_output
    assert "\x1b[?25h" in result.terminal_output


def _assert_renderer_restored(result: _PtyResult) -> None:
    assert _without_pendin(result.after) == _without_pendin(result.before)
    assert "\x1b[?25h" in result.terminal_output


def _assert_markdown_content(text: str) -> None:
    assert "bold" in text
    assert "Final" in text
    assert "code" in text
    assert all(marker in text for marker in ("key", "value", "one", "two"))


def _markdown_content_signature(text: str) -> str:
    start = text.index("Intro")
    end = text.index("code", start) + len("code")
    visible = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text[start:end])
    return " ".join(visible.split())


def _without_pendin(attrs: list[Any]) -> list[Any]:
    normalized = list(attrs)
    pendin = getattr(termios, "PENDIN", 0)
    normalized[3] &= ~pendin
    return normalized


def _serialize_termios_attrs(attrs: list[Any]) -> list[Any]:
    control_chars = [
        value[0] if isinstance(value, bytes) else value for value in attrs[6]
    ]
    return [*attrs[:6], control_chars]


def _deserialize_termios_attrs(attrs: list[Any]) -> list[Any]:
    return [*attrs[:6], [bytes((value,)) for value in attrs[6]]]


def _read_until(
    fd: int,
    needle: bytes,
    buffer: bytearray,
    *,
    start: int = 0,
) -> int:
    deadline = time.monotonic() + _PTY_TIMEOUT
    while True:
        found = buffer.find(needle, start)
        if found >= 0:
            return found + len(needle)

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
                raise AssertionError(
                    f"PTY closed before {needle!r}; output={buffer!r}"
                ) from exc
            raise
        if not chunk:
            raise AssertionError(f"PTY closed before {needle!r}; output={buffer!r}")
        buffer.extend(chunk)
        _respond_to_cursor_queries(fd, chunk)


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
            raise AssertionError("timed out waiting for CLI PTY child")

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
            _respond_to_cursor_queries(master_fd, chunk)


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


def _respond_to_cursor_queries(fd: int, chunk: bytes) -> None:
    for _ in range(chunk.count(b"\x1b[6n")):
        try:
            os.write(fd, b"\x1b[1;1R")
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_json(fd: int, value: dict[str, Any]) -> None:
    payload = json.dumps(value).encode("utf-8")
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])
