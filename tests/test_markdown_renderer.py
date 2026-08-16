"""Unit tests for the TTY Markdown presentation adapter."""

from __future__ import annotations

from io import StringIO

import pytest

from cli_agent.presentation import MarkdownStreamRenderer, render_event
from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    TextDelta,
    ToolCall,
    ToolCallReady,
)


class _TerminalOutput(StringIO):
    def isatty(self) -> bool:
        return True


def test_renderer_renders_markdown_styles_and_table() -> None:
    output = _TerminalOutput()
    renderer = MarkdownStreamRenderer(output)

    renderer.feed(
        "# Title\n\n**bold** and `code`\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |"
    )
    renderer.finish()

    value = output.getvalue()
    assert "\033[1m" in value
    assert "bold" in value
    assert "code" in value
    assert all(marker in value for marker in ("Title", "a", "b", "1", "2"))
    assert "| a | b |" not in value


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
