"""Issue 010: resumable Session workflows exposed by the CLI Host."""

import asyncio
from io import StringIO
from pathlib import Path

from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime._resources as resources_module
from cli_agent.config import CliConfig
from cli_agent.errors import SessionPersistenceError
from cli_agent.presentation import render_host_error
from cli_agent.runner import run_agent
from cli_agent.runtime import ContextPolicy, ScriptedModelProvider
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime.runtime import AgentRuntime

_CONTEXT_POLICY = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def _config(workspace: Path) -> CliConfig:
    return CliConfig(
        task=None,
        workspace=workspace,
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=16_384,
        output_reserve_tokens=2_048,
        safety_margin_tokens=4_096,
    )


def _install_database(monkeypatch, path: Path) -> None:
    class _TestStateDatabase:
        @classmethod
        def open(cls, requested: object = None) -> _StateDatabase:
            del requested
            return _StateDatabase.open(path / "state.sqlite3")

    monkeypatch.setattr(resources_module, "_StateDatabase", _TestStateDatabase)


def test_cli_can_list_resume_and_create_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_database(monkeypatch, tmp_path)

    async def scenario() -> tuple[int, str, str]:
        async with await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_ScriptedInteraction("deny"),
            context_policy=_CONTEXT_POLICY,
        ) as runtime:
            target = await runtime.new_session()
            await runtime.detach_session()

        stderr = StringIO()
        exit_code = await run_agent(
            _config(tmp_path),
            ScriptedModelProvider(script=()),
            stdin=StringIO(
                "/sessions\n"
                f"/resume {target.session_id}\n"
                "/new\n"
                "/sessions\n"
                "/exit\n"
            ),
            stdout=StringIO(),
            stderr=stderr,
        )
        return exit_code, target.session_id, stderr.getvalue()

    exit_code, target_id, output = asyncio.run(scenario())

    assert exit_code == 0
    assert target_id in output
    assert "archive" not in output
    assert "delete-session" not in output
    metadata_lines = [
        line for line in output.splitlines() if line.startswith("[session] id=")
    ]
    assert len(metadata_lines) == 5


def test_session_list_and_host_errors_are_safe_and_stable(tmp_path: Path) -> None:
    stderr = StringIO()
    render_host_error(
        SessionPersistenceError(operation="list", exception_type="OperationalError"),
        stderr=stderr,
    )

    assert stderr.getvalue() == (
        "[error] code=session_persistence_failed "
        "Session persistence failed; check storage and retry.\n"
    )
    assert "OperationalError" not in stderr.getvalue()
