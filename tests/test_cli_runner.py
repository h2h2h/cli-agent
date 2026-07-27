import asyncio
import shlex
import sys
from io import StringIO
from pathlib import Path

import pytest

from cli_agent.config import CliConfig
from cli_agent.presentation import render_event
from cli_agent.runner import run_agent
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ModelCompletion,
    ModelUsage,
    ScriptedModelProvider,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResultMessage,
    UserMessage,
)


def test_runs_and_presents_one_agent_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    call = ToolCall(
        call_id="inspect_workspace",
        name="exec",
        arguments={
            "command": _python_command("print('workspace inspected')"),
        },
    )
    tool_message = AssistantMessage(
        content=(TextBlock(text="Inspecting. "), call),
    )
    final_message = AssistantMessage.text("Inspection complete.")
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Inspecting. "),
                ToolCallReady(call=call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            ),
            (
                TextDelta(text="Inspection complete."),
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                    usage=ModelUsage(
                        input_tokens=12,
                        output_tokens=3,
                        total_tokens=15,
                    ),
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = StringIO()
    lifecycle: list[str] = []
    original_close_session = AgentRuntime.close_session
    original_close = AgentRuntime.close

    async def tracking_close_session(
        runtime: AgentRuntime,
        session_id: str,
    ) -> None:
        lifecycle.append("session")
        await original_close_session(runtime, session_id)

    async def tracking_close(runtime: AgentRuntime) -> None:
        lifecycle.append("runtime")
        await original_close(runtime)

    monkeypatch.setattr(AgentRuntime, "close_session", tracking_close_session)
    monkeypatch.setattr(AgentRuntime, "close", tracking_close)

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task="Inspect the Workspace"),
            provider,
            stdin=StringIO(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Inspecting. Inspection complete."
    assert stderr.getvalue() == (
        "[tool] exec\n[completion] reason=stop usage=input:12,output:3,total:15\n"
    )
    assert lifecycle == ["session", "runtime"]

    first_request, second_request = provider.requests
    system_message = first_request.messages[0]
    assert isinstance(system_message, SystemMessage)
    assert first_request.messages == (
        system_message,
        UserMessage.text("Inspect the Workspace"),
    )
    result_message = second_request.messages[-1]
    assert isinstance(result_message, ToolResultMessage)
    result = result_message.content[0]
    assert isinstance(result.output, dict)
    assert result.output["status"] == "exited"
    provider.assert_exhausted()


def test_returns_failure_when_model_stream_has_no_completion(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="Partial response"),),),
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path),
            provider,
            stdin=StringIO(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 1
    assert stdout.getvalue() == "Partial response"
    assert stderr.getvalue() == ("cli-agent: model stream ended without a completion\n")
    provider.assert_exhausted()


def test_runs_multiple_interactive_turns_in_one_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_user = UserMessage.text("First turn")
    second_user = UserMessage.text("Second turn")
    first_assistant = AssistantMessage.text("First response")
    second_assistant = AssistantMessage.text("Second response")
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="First response"),
                ModelCompletion(
                    message=first_assistant,
                    finish_reason="stop",
                ),
            ),
            (
                TextDelta(text="Second response"),
                ModelCompletion(
                    message=second_assistant,
                    finish_reason="stop",
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = StringIO()
    lifecycle: list[str] = []
    original_close_session = AgentRuntime.close_session
    original_close = AgentRuntime.close

    async def tracking_close_session(
        runtime: AgentRuntime,
        session_id: str,
    ) -> None:
        lifecycle.append("session")
        await original_close_session(runtime, session_id)

    async def tracking_close(runtime: AgentRuntime) -> None:
        lifecycle.append("runtime")
        await original_close(runtime)

    monkeypatch.setattr(AgentRuntime, "close_session", tracking_close_session)
    monkeypatch.setattr(AgentRuntime, "close", tracking_close)

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=StringIO("\nFirst turn\nSecond turn\n:q\n"),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "First response\nSecond response\n"
    assert stderr.getvalue() == ("[completion] reason=stop\n[completion] reason=stop\n")
    assert lifecycle == ["session", "runtime"]

    first_request, second_request = provider.requests
    system_message = first_request.messages[0]
    assert isinstance(system_message, SystemMessage)
    assert first_request.messages == (system_message, first_user)
    assert second_request.messages == (
        system_message,
        first_user,
        first_assistant,
        second_user,
    )
    provider.assert_exhausted()


def test_interactive_terminal_prompts_until_quit(tmp_path: Path) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    provider = ScriptedModelProvider(script=())
    stdout = StringIO()
    stderr = StringIO()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=TerminalInput("\n:q\n"),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "cli-agent> cli-agent> "
    assert provider.requests == ()
    provider.assert_exhausted()


def test_interactive_interrupt_closes_session_and_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class InterruptingInput(StringIO):
        def readline(self, *args: object, **kwargs: object) -> str:
            raise KeyboardInterrupt

    provider = ScriptedModelProvider(script=())
    lifecycle: list[str] = []
    original_close_session = AgentRuntime.close_session
    original_close = AgentRuntime.close

    async def tracking_close_session(
        runtime: AgentRuntime,
        session_id: str,
    ) -> None:
        lifecycle.append("session")
        await original_close_session(runtime, session_id)

    async def tracking_close(runtime: AgentRuntime) -> None:
        lifecycle.append("runtime")
        await original_close(runtime)

    monkeypatch.setattr(AgentRuntime, "close_session", tracking_close_session)
    monkeypatch.setattr(AgentRuntime, "close", tracking_close)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            run_agent(
                _config(tmp_path, task=None),
                provider,
                stdin=InterruptingInput(),
                stdout=StringIO(),
                stderr=StringIO(),
            )
        )

    assert lifecycle == ["session", "runtime"]
    provider.assert_exhausted()


def test_renderer_does_not_duplicate_completed_assistant_text() -> None:
    stdout = StringIO()
    stderr = StringIO()
    call = ToolCall(
        call_id="call_1",
        name="exec",
        arguments={"command": "pwd"},
    )

    results = (
        render_event(
            TextDelta(text="Streamed answer"),
            stdout=stdout,
            stderr=stderr,
        ),
        render_event(
            ToolCallReady(call=call),
            stdout=stdout,
            stderr=stderr,
        ),
        render_event(
            ModelCompletion(
                message=AssistantMessage.text("Streamed answer"),
                finish_reason="stop",
            ),
            stdout=stdout,
            stderr=stderr,
        ),
    )

    assert results == (None, None, None)
    assert stdout.getvalue() == "Streamed answer"
    assert stderr.getvalue() == "[tool] exec\n[completion] reason=stop\n"


def _config(
    tmp_path: Path,
    *,
    task: str | None = "Run the task",
) -> CliConfig:
    return CliConfig(
        task=task,
        workspace=tmp_path,
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
    )


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
