"""Unit tests for the project-owned TUI session."""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import cli_agent.tui.session as session_module
from cli_agent.slash_commands import specs
from cli_agent.tui import TuiSession
from cli_agent.tui.completer import _SlashCommandCompleter

_CTRL_J = "\n"


@pytest.fixture
def pipe_session(monkeypatch):
    with create_pipe_input() as input_stream:
        monkeypatch.setattr(
            session_module,
            "create_input",
            lambda stdin: input_stream,
        )
        monkeypatch.setattr(
            session_module,
            "create_output",
            lambda stderr: DummyOutput(),
        )
        session = TuiSession(
            stdin=StringIO(),
            stderr=StringIO(),
            specs=specs,
        )
        yield session, input_stream
        asyncio.run(session.close())


def test_public_api_exports_only_tui_session() -> None:
    import cli_agent.tui as tui

    assert tui.__all__ == ["TuiSession"]
    assert tui.TuiSession is TuiSession


def test_enter_and_ctrl_j_edit_multiline_text(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        input_stream.send_text(f"first{_CTRL_J}second\r")
        return await session.read_text("task> ")

    assert asyncio.run(scenario()) == "first\nsecond"


def test_cursor_movement_and_delete(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> tuple[str | None, str | None]:
        input_stream.send_text("ac\x1b[Db\r")
        inserted = await session.read_text("task> ")

        input_stream.send_text("abcd\x1b[D\x1b[D\x1b[3~\r")
        deleted = await session.read_text("task> ")
        return inserted, deleted

    assert asyncio.run(scenario()) == ("abc", "abd")


def test_history_is_kept_for_the_lifetime_of_the_session(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> tuple[str | None, str | None]:
        input_stream.send_text("first\r")
        first = await session.read_text("task> ")

        input_stream.send_text("\x10\r")
        recalled = await session.read_text("task> ")
        return first, recalled

    assert asyncio.run(scenario()) == ("first", "first")


def test_bracketed_paste_keeps_newlines(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        input_stream.send_text("\x1b[200~line one\nline two\x1b[201~\r")
        return await session.read_text("task> ")

    assert asyncio.run(scenario()) == "line one\nline two"


def test_first_ctrl_c_clears_and_second_ctrl_c_interrupts(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        input_stream.send_text("draft\x03after\r")
        cleared = await session.read_text("task> ")

        input_stream.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            await session.read_text("task> ")
        return cleared

    assert asyncio.run(scenario()) == "after"


def test_ctrl_d_on_empty_input_returns_eof(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        input_stream.send_text("\x04")
        return await session.read_text("task> ")

    assert asyncio.run(scenario()) is None


def test_confirm_accepts_only_yes(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> tuple[bool, bool, bool]:
        input_stream.send_text("y\rYES\rno\r")
        return (
            await session.confirm("allow> "),
            await session.confirm("allow> "),
            await session.confirm("allow> "),
        )

    assert asyncio.run(scenario()) == (True, True, False)


def test_read_and_confirm_are_serialized(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> tuple[str | None, bool]:
        task = asyncio.create_task(session.read_text("task> "))
        confirmation = asyncio.create_task(session.confirm("allow> "))
        await asyncio.sleep(0)

        input_stream.send_text("task\r")
        task_result = await task
        input_stream.send_text("yes\r")
        confirmation_result = await confirmation
        return task_result, confirmation_result

    assert asyncio.run(scenario()) == ("task", True)


def test_keyboard_interrupt_releases_session_lock(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        input_stream.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            await session.read_text("task> ")

        input_stream.send_text("next\r")
        return await session.read_text("task> ")

    assert asyncio.run(scenario()) == "next"


def test_close_is_idempotent_and_prevents_new_prompts(pipe_session) -> None:
    session, _ = pipe_session

    async def scenario() -> None:
        await session.close()
        await session.close()
        with pytest.raises(RuntimeError, match="TuiSession is closed"):
            await session.read_text("task> ")

    asyncio.run(scenario())


def test_close_cancels_a_pending_prompt(pipe_session) -> None:
    session, _ = pipe_session

    async def scenario() -> None:
        pending = asyncio.create_task(session.read_text("task> "))
        await asyncio.sleep(0)
        await session.close()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())


def _completions(text: str) -> list:
    completer = _SlashCommandCompleter(specs)
    return list(completer.get_completions(Document(text), CompleteEvent()))


def test_completer_suggests_exit_and_usage_for_bare_slash() -> None:
    completions = _completions("/")
    assert [c.text for c in completions] == [
        "/exit",
        "/usage",
        "/new",
        "/sessions",
        "/resume",
    ]
    assert [c.start_position for c in completions] == [-1] * 5
    assert completions[0].display_text == "/exit"
    assert completions[0].display_meta_text == specs[0].description
    assert completions[1].display_text == "/usage"
    assert completions[1].display_meta_text == specs[1].description


def test_completer_filters_by_case_insensitive_prefix() -> None:
    assert [c.text for c in _completions("/e")] == ["/exit"]
    assert [c.text for c in _completions("/E")] == ["/exit"]
    assert [c.text for c in _completions("/exi")] == ["/exit"]
    assert [c.text for c in _completions("/u")] == ["/usage"]
    assert [c.text for c in _completions("/usage")] == ["/usage"]
    assert _completions("/z") == []
    assert _completions("") == []


def test_completer_ignores_slash_commands_outside_buffer_start() -> None:
    assert _completions("foo /") == []
    assert _completions("foo /e") == []
    assert _completions("first\n/e") == []


def test_completer_ignores_fragments_with_whitespace() -> None:
    assert _completions("/exit ") == []
    assert _completions("/e x") == []


def test_completer_replaces_the_typed_fragment() -> None:
    completion = _completions("/e")[0]
    assert completion.text == "/exit"
    assert completion.start_position == -2


def test_tab_applies_selected_completion_and_keeps_editing(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        pending = asyncio.create_task(session.read_text("task> "))
        input_stream.send_text("/e")
        await asyncio.sleep(0.2)
        input_stream.send_text("\t now\r")
        return await pending

    assert asyncio.run(scenario()) == "/exit now"


def test_tab_without_menu_keeps_existing_behavior(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        pending = asyncio.create_task(session.read_text("task> "))
        input_stream.send_text("foo\tbar\r")
        return await pending

    assert asyncio.run(scenario()) == "foobar"


def test_enter_with_open_menu_submits_buffer_without_completion(
    pipe_session,
) -> None:
    session, input_stream = pipe_session

    async def scenario() -> str | None:
        pending = asyncio.create_task(session.read_text("task> "))
        input_stream.send_text("/e")
        await asyncio.sleep(0.2)
        input_stream.send_text("\r")
        return await pending

    assert asyncio.run(scenario()) == "/e"


def test_escape_closes_menu_and_keeps_buffer(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> tuple[bool, bool, str | None]:
        pending = asyncio.create_task(session.read_text("task> "))
        input_stream.send_text("/e")
        await asyncio.sleep(0.2)
        app = get_app()
        opened = app.layout.current_buffer.complete_state is not None

        input_stream.send_text("\x1b")
        await asyncio.sleep(0.7)
        closed = app.layout.current_buffer.complete_state is None

        input_stream.send_text("x\r")
        return opened, closed, await pending

    assert asyncio.run(scenario()) == (True, True, "/ex")


def test_backspace_reopens_completion_menu(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> tuple[bool, bool, str | None]:
        pending = asyncio.create_task(session.read_text("task> "))
        input_stream.send_text("/e")
        await asyncio.sleep(0.2)
        app = get_app()
        opened = app.layout.current_buffer.complete_state is not None

        input_stream.send_text("z")
        await asyncio.sleep(0.2)
        closed = app.layout.current_buffer.complete_state is None

        input_stream.send_text("\x7f")
        await asyncio.sleep(0.2)
        reopened = app.layout.current_buffer.complete_state is not None

        input_stream.send_text("\r")
        await pending
        return opened, closed, reopened

    assert asyncio.run(scenario()) == (True, True, True)


def test_confirm_does_not_offer_slash_command_candidates(pipe_session) -> None:
    session, input_stream = pipe_session

    async def scenario() -> tuple[bool, bool]:
        pending = asyncio.create_task(session.confirm("allow> "))
        input_stream.send_text("/e")
        await asyncio.sleep(0.2)
        app = get_app()
        menu_open = app.layout.current_buffer.complete_state is not None

        input_stream.send_text("\r")
        return menu_open, await pending

    assert asyncio.run(scenario()) == (False, False)
