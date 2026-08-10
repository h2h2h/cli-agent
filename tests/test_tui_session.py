"""Unit tests for the project-owned TUI session."""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import cli_agent.tui.session as session_module
from cli_agent.tui import TuiSession

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
        session = TuiSession(stdin=StringIO(), stderr=StringIO())
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
