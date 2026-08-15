"""End-to-end durable session persistence through the public Runtime.

These tests drive the full Runtime order - create, run_turn appends,
close_session - with scripted providers over a real test state database
and pin the journal semantics: original message content, compaction
boundaries, and fail-closed writes.
"""

import asyncio
import json
import shlex
import sqlite3
import sys
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime._resources as resources_module
from cli_agent.errors import HostFacingError
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    ToolCall,
    ToolCallReady,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

_user_interaction = _ScriptedInteraction("deny")

_STANDARD_BUDGET = ContextPolicy(
    context_window_tokens=128_000,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
    minimum_reclaim_tokens=1,
)


def _install_test_state_database(
    monkeypatch,
    tmp_path: Path,
) -> list[_StateDatabase]:
    instances: list[_StateDatabase] = []

    class _TestStateDatabase:
        @classmethod
        def open(cls, path: object = None) -> _StateDatabase:
            del path
            database = _StateDatabase.open(tmp_path / "state.sqlite3")
            instances.append(database)
            return database

    monkeypatch.setattr(resources_module, "_StateDatabase", _TestStateDatabase)
    return instances


def _completion(message: AssistantMessage) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop")


def _plain_step(text: str) -> tuple[ModelEvent, ...]:
    return (_completion(AssistantMessage.text(text)),)


def _exec_step(call: ToolCall) -> tuple[ModelEvent, ...]:
    return (
        ToolCallReady(call=call),
        _completion(AssistantMessage(content=(call,))),
    )


def _tool_call(call_id: str, command: str) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="exec",
        arguments={"command": command, "wait_ms": 20_000},
    )


def _print_command(char_count: int) -> str:
    source = f"print('x' * {char_count})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


async def _open_runtime(
    tmp_path: Path,
    provider,
    received: list[RuntimeDiagnostic] | None = None,
) -> AgentRuntime:
    return await AgentRuntime.open(
        workspace=tmp_path,
        provider=provider,
        user_interaction=_user_interaction,
        context_policy=_STANDARD_BUDGET,
        on_diagnostic=received.append if received is not None else None,
    )


async def _run_turn(
    runtime: AgentRuntime,
    session_id: str,
    text: str,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(
                session_id,
                UserMessage.text(text),
            )
        ]
    )


def _sessions(path: Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT session_id, workspace_id, config, created_at, updated_at, "
        "archived_at FROM sessions ORDER BY session_id"
    ).fetchall()
    connection.close()
    return rows


def _messages(path: Path, session_id: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT revision, role, payload, created_at FROM session_journal "
        "WHERE session_id = ? ORDER BY revision",
        (session_id,),
    ).fetchall()
    connection.close()
    return rows


def _session_config_prompt(row: tuple) -> str:
    return json.loads(row[2])["system_prompt"]


def test_run_turn_persists_session_trace_in_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_test_state_database(monkeypatch, tmp_path)
    call = _tool_call("call_echo", "echo hello")
    provider = ScriptedModelProvider(
        script=(
            _exec_step(call),
            _plain_step("echoed"),
        )
    )

    async def scenario() -> None:
        async with await _open_runtime(tmp_path, provider) as runtime:
            await _run_turn(runtime, "s1", "hello")

        (session,) = _sessions(tmp_path / "state.sqlite3")
        session_id, workspace_id, config, created_at, updated_at, archived_at = session
        assert session_id == "s1"
        assert workspace_id == runtime._resources.workspace.id
        assert workspace_id.startswith("local:")
        assert json.loads(_session_config_prompt(session))["blocks"][0][
            "text"
        ].startswith("You are cli-agent")
        assert json.loads(config)["schema_version"] == 1
        assert archived_at is None
        rows = _messages(tmp_path / "state.sqlite3", "s1")
        assert [(row[0], row[1]) for row in rows] == [
            (1, "user"),
            (2, "assistant"),
            (3, "tool_result"),
            (4, "assistant"),
        ]
        assert json.loads(rows[0][2]) == {
            "schema_version": 1,
            "role": "user",
            "blocks": [{"type": "text", "text": "hello"}],
        }
        assistant_blocks = json.loads(rows[1][2])["blocks"]
        assert assistant_blocks == [
            {
                "type": "tool_call",
                "call_id": "call_echo",
                "name": "exec",
                "arguments": {"command": "echo hello", "wait_ms": 20_000},
            }
        ]
        tool_result = json.loads(rows[2][2])["results"][0]
        assert tool_result["call_id"] == "call_echo"
        assert tool_result["output"]["exit_code"] == 0
        assert tool_result["error"] is None
        assert json.loads(rows[3][2]) == {
            "schema_version": 1,
            "role": "assistant",
            "blocks": [{"type": "text", "text": "echoed"}],
        }
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_system_prompt_persists_final_workspace_instructions_section(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_test_state_database(monkeypatch, tmp_path)
    rules = "# Project rules\n\nrun `uv run pytest` before review.\n"
    (tmp_path / "AGENTS.md").write_text(rules, encoding="utf-8")
    provider = ScriptedModelProvider(script=(_plain_step("done"),))

    async def scenario() -> None:
        async with await _open_runtime(tmp_path, provider) as runtime:
            await _run_turn(runtime, "s1", "hello")

        (session,) = _sessions(tmp_path / "state.sqlite3")
        system_prompt = json.loads(_session_config_prompt(session))["blocks"][0]["text"]
        assert "**Workspace instructions**" in system_prompt
        assert f"Source: {tmp_path.resolve() / 'AGENTS.md'}" in system_prompt
        assert rules in system_prompt
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_tier3_summary_stays_out_of_session_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_test_state_database(monkeypatch, tmp_path)
    summary_text = (
        "## Progress\ninspected the workspace\n"
        "## Files\nreport.txt located\n"
        "## Todo\nverify the fix\n"
        "## Context\nuser wants concise answers"
    )
    provider = ScriptedModelProvider(
        script=(
            _plain_step("x" * 480_000),
            _plain_step("x" * 80_000),
            (_completion(AssistantMessage.text(summary_text)),),
            _plain_step("final answer"),
        )
    )

    async def scenario() -> None:
        async with await _open_runtime(tmp_path, provider) as runtime:
            await _run_turn(runtime, "s1", "First old discussion")
            await _run_turn(runtime, "s1", "Second recent discussion")
            await _run_turn(runtime, "s1", "Wrap up")

        rows = _messages(tmp_path / "state.sqlite3", "s1")
        assert [(row[1]) for row in rows] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert json.loads(rows[1][2])["blocks"][0]["text"] == "x" * 480_000
        assert not any(summary_text in row[2] for row in rows)
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_reduced_tool_result_keeps_original_payload_in_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_test_state_database(monkeypatch, tmp_path)
    old_call = _tool_call("call_old", _print_command(300_000))
    recent_call = _tool_call("call_recent", _print_command(50_000))
    provider = ScriptedModelProvider(
        script=(
            _exec_step(old_call),
            _plain_step("old turn done"),
            _exec_step(recent_call),
            _plain_step("recent turn done"),
            _plain_step("final answer"),
        )
    )

    async def scenario() -> None:
        async with await _open_runtime(tmp_path, provider) as runtime:
            await _run_turn(runtime, "s1", "Inspect the old workspace")
            await _run_turn(runtime, "s1", "Inspect the recent marker")
            await _run_turn(runtime, "s1", "Summarize the state")

        final_request = provider.requests[4]
        old_result = next(
            message.content[0]
            for message in final_request.messages
            if isinstance(message, ToolResultMessage) and message.content
        )
        output = old_result.output
        assert isinstance(output, dict)
        assert output.get("reclaimed", {}).get("state") == "snipped"

        rows = _messages(tmp_path / "state.sqlite3", "s1")
        tool_results = [row for row in rows if row[1] == "tool_result"]
        assert len(tool_results) == 2
        old_payload = json.loads(tool_results[0][2])["results"][0]
        assert old_payload["call_id"] == "call_old"
        assert "reclaimed" not in old_payload["output"]
        chunks = old_payload["output"]["chunks"]
        assert sum(len(chunk["text"]) for chunk in chunks) >= 300_000
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_unwritable_database_prevents_session_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_test_state_database(monkeypatch, tmp_path)

    class _UnwritableDatabase:
        def transaction(self):
            raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(
        resources_module,
        "SessionStore",
        lambda database: SessionStore(_UnwritableDatabase()),  # type: ignore[arg-type]
    )
    provider = ScriptedModelProvider(script=())
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        runtime = await _open_runtime(tmp_path, provider, received)
        try:
            with pytest.raises(HostFacingError) as raised:
                await _run_turn(runtime, "s1", "First turn")
            await runtime.close_session("s1")
        finally:
            await runtime.close()

        assert raised.value.code == "session_persistence_failed"
        assert raised.value.details == {
            "operation": "create",
            "session_id": "s1",
            "exception_type": "OperationalError",
        }
        assert received == []
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_close_session_persists_no_lifecycle_state(tmp_path: Path, monkeypatch) -> None:
    _install_test_state_database(monkeypatch, tmp_path)
    provider = ScriptedModelProvider(script=(_plain_step("done"),))

    async def scenario() -> None:
        runtime = await _open_runtime(tmp_path, provider)
        try:
            await _run_turn(runtime, "s1", "hello")
            (session,) = _sessions(tmp_path / "state.sqlite3")
            before = (session[4], session[5])

            await runtime.close_session("s1")

            (closed_session,) = _sessions(tmp_path / "state.sqlite3")
            assert (closed_session[4], closed_session[5]) == before
            assert closed_session[5] is None
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_existing_session_id_requires_resume_after_runtime_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_test_state_database(monkeypatch, tmp_path)
    first_provider = ScriptedModelProvider(script=(_plain_step("done"),))
    reopened_provider = ScriptedModelProvider(script=())

    async def scenario() -> None:
        async with await _open_runtime(tmp_path, first_provider) as runtime:
            await _run_turn(runtime, "s1", "hello")

        async with await _open_runtime(tmp_path, reopened_provider) as reopened:
            with pytest.raises(HostFacingError) as raised:
                await _run_turn(reopened, "s1", "must resume")

        assert raised.value.code == "session_already_exists"
        assert raised.value.details == {"session_id": "s1"}
        rows = _messages(tmp_path / "state.sqlite3", "s1")
        assert [(row[0], row[1]) for row in rows] == [
            (1, "user"),
            (2, "assistant"),
        ]
        first_provider.assert_exhausted()
        reopened_provider.assert_exhausted()

    asyncio.run(scenario())
