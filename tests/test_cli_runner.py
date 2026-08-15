import asyncio
import shlex
import sys
import threading
from collections.abc import AsyncIterator
from io import StringIO
from pathlib import Path

import pytest
from policy_fakes import _AskExecutablePolicy

import cli_agent.runner as runner_module
from cli_agent.config import CliConfig
from cli_agent.presentation import (
    render_diagnostic,
    render_event,
    render_session_usage,
)
from cli_agent.runner import run_agent
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    ModelUsage,
    RuntimeDiagnostic,
    ScriptedModelProvider,
    SessionUsage,
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
    session_ids: list[str] = []
    original_new_session = AgentRuntime.new_session
    original_detach_session = AgentRuntime.detach_session
    original_close = AgentRuntime.close

    async def tracking_new_session(
        runtime: AgentRuntime,
        *,
        provider: object = None,
    ) -> object:
        session = await original_new_session(runtime, provider=provider)
        session_ids.append(session.session_id)
        return session

    async def tracking_detach_session(runtime: AgentRuntime) -> None:
        lifecycle.append("session")
        await original_detach_session(runtime)

    async def tracking_close(runtime: AgentRuntime) -> None:
        lifecycle.append("runtime")
        await original_close(runtime)

    monkeypatch.setattr(AgentRuntime, "new_session", tracking_new_session)
    monkeypatch.setattr(AgentRuntime, "detach_session", tracking_detach_session)
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
        f"[tool] exec: {call.arguments['command']}\n"
        "[completion] reason=stop usage=input:12,output:3,total:15\n"
        f"[session] {session_ids[0]}\n"
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


def test_reference_cli_cancels_unfinished_library_summary(tmp_path: Path) -> None:
    class BlockingLibraryProvider:
        def __init__(self) -> None:
            self.summary_started = asyncio.Event()
            self.summary_cancelled = asyncio.Event()

        async def generate(
            self,
            request: ModelRequest,
        ) -> AsyncIterator[ModelEvent]:
            if request.tools == ():
                self.summary_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.summary_cancelled.set()
                    raise
                return
            await self.summary_started.wait()
            yield ModelCompletion(
                message=AssistantMessage.text("Turn complete."),
                finish_reason="stop",
            )

    repertoire = tmp_path.parent / f"{tmp_path.name}-repertoire"
    (repertoire / "library").mkdir(parents=True)
    (repertoire / "library" / "slow.md").write_text(
        "content\n",
        encoding="utf-8",
    )
    provider = BlockingLibraryProvider()

    async def scenario() -> int:
        return await asyncio.wait_for(
            run_agent(
                _config(tmp_path, repertoire=repertoire),
                provider,
                stdin=StringIO(),
                stdout=StringIO(),
                stderr=StringIO(),
            ),
            timeout=1,
        )

    assert asyncio.run(scenario()) == 0
    assert provider.summary_started.is_set()
    assert provider.summary_cancelled.is_set()


def test_interactive_input_does_not_block_library_summaries(tmp_path: Path) -> None:
    summaries_completed = threading.Event()

    class DirectorySummaryProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self,
            request: ModelRequest,
        ) -> AsyncIterator[ModelEvent]:
            assert request.tools == ()
            self.calls += 1
            if self.calls == 3:
                summaries_completed.set()
            yield ModelCompletion(
                message=AssistantMessage.text(f"Summary {self.calls}."),
                finish_reason="stop",
            )

    class WaitingInput(StringIO):
        def readline(self, *args: object, **kwargs: object) -> str:
            if not summaries_completed.wait(timeout=1):
                raise AssertionError(
                    "Library summaries did not run while the CLI awaited input"
                )
            return ":q\n"

    repertoire = tmp_path.parent / f"{tmp_path.name}-repertoire"
    source = repertoire / "library" / "notes" / "guide.md"
    source.parent.mkdir(parents=True)
    source.write_text("Guide content.\n", encoding="utf-8")
    provider = DirectorySummaryProvider()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None, repertoire=repertoire),
            provider,
            stdin=WaitingInput(),
            stdout=StringIO(),
            stderr=StringIO(),
        )
    )

    assert exit_code == 0
    assert provider.calls == 3
    library = tmp_path / ".workspace" / "library"
    assert "status: ready" in (library / "notes" / "index.md").read_text(
        encoding="utf-8"
    )
    assert "status: ready" in (library / "index.md").read_text(encoding="utf-8")


def test_returns_failure_when_model_stream_has_no_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="Partial response"),),),
    )
    sessions = _install_session_capture(monkeypatch)
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
    assert stderr.getvalue() == (
        "cli-agent: model stream ended without a completion\n"
        f"[session] {sessions[0]}\n"
    )
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
    session_ids: list[str] = []
    original_new_session = AgentRuntime.new_session
    original_detach_session = AgentRuntime.detach_session
    original_close = AgentRuntime.close

    async def tracking_new_session(
        runtime: AgentRuntime,
        *,
        provider: object = None,
    ) -> object:
        session = await original_new_session(runtime, provider=provider)
        session_ids.append(session.session_id)
        return session

    async def tracking_detach_session(runtime: AgentRuntime) -> None:
        lifecycle.append("session")
        await original_detach_session(runtime)

    async def tracking_close(runtime: AgentRuntime) -> None:
        lifecycle.append("runtime")
        await original_close(runtime)

    monkeypatch.setattr(AgentRuntime, "new_session", tracking_new_session)
    monkeypatch.setattr(AgentRuntime, "detach_session", tracking_detach_session)
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
    assert stderr.getvalue() == (
        "[completion] reason=stop\n"
        "[completion] reason=stop\n"
        f"[session] {session_ids[0]}\n"
    )
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


@pytest.mark.parametrize(
    ("interaction_input", "expected_exists", "expected_error"),
    (
        ("yes\n", False, None),
        ("\n", True, "direct invocation of 'rm' requires Host approval"),
    ),
)
def test_reference_cli_resolves_ask_interaction_once(
    tmp_path: Path,
    monkeypatch,
    interaction_input: str,
    expected_exists: bool,
    expected_error: str | None,
) -> None:
    proof = tmp_path / "approval-proof.txt"
    proof.write_text("preserved", encoding="utf-8")
    command = "rm approval-proof.txt"
    call = ToolCall(
        call_id="review_rm",
        name="exec",
        arguments={"command": command},
    )
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=call),
                ModelCompletion(
                    message=AssistantMessage(content=(call,)),
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=AssistantMessage.text("Reviewed."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    stderr = StringIO()
    sessions = _install_session_capture(monkeypatch)

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path),
            provider,
            execution_policy=_AskExecutablePolicy(
                frozenset({"rm"}),
                rule_id="test.ask-rm",
                reason="direct invocation of 'rm' requires Host approval",
            ),
            stdin=StringIO(interaction_input),
            stdout=StringIO(),
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert proof.exists() is expected_exists
    assert stderr.getvalue() == (
        f"[tool] exec: {command}\n"
        "[interaction] direct invocation of 'rm' requires Host approval\n"
        f"command: {command}\n"
        "Allow once? [y/N] \n"
        "[completion] reason=stop\n"
        f"[session] {sessions[0]}\n"
    )
    result_message = provider.requests[1].messages[-1]
    assert isinstance(result_message, ToolResultMessage)
    result = result_message.content[0]
    if expected_error is None:
        assert isinstance(result.output, dict)
        assert result.output["status"] == "exited"
        assert result.error is None
    else:
        assert result.output is None
        assert isinstance(result.error, dict)
        assert result.error["message"] == expected_error
    provider.assert_exhausted()


def test_interactive_terminal_prompts_until_quit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self.read_prompts: list[str] = []
            self.closed = False
            self._responses = iter(("", ":q"))
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str:
            self.read_prompts.append(prompt)
            return next(self._responses)

        async def confirm(self, prompt: str) -> bool:
            del prompt
            raise AssertionError("confirmation was not expected")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)
    provider = ScriptedModelProvider(script=())
    sessions = _install_session_capture(monkeypatch)
    stdout = StringIO()
    stderr = _TerminalOutput()

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
    assert stderr.getvalue() == (
        f"\033[2;36m[session] {sessions[0]}\033[0m\n"
    )
    tui_session = FakeTuiSession.instances[0]
    assert tui_session.read_prompts == ["cli-agent> ", "cli-agent> "]
    assert tui_session.closed is True
    assert provider.requests == ()
    provider.assert_exhausted()


def test_tty_slash_exit_ends_session_without_agent_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self.closed = False
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str | None:
            del prompt
            return "/exit"

        async def confirm(self, prompt: str) -> bool:
            del prompt
            raise AssertionError("confirmation was not expected")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)
    provider = ScriptedModelProvider(script=())
    sessions = _install_session_capture(monkeypatch)
    stdout = StringIO()
    stderr = _TerminalOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=TerminalInput(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        f"\033[2;36m[session] {sessions[0]}\033[0m\n"
    )
    assert FakeTuiSession.instances[0].closed is True
    assert provider.requests == ()
    provider.assert_exhausted()


@pytest.mark.parametrize(
    "task",
    ("/exit now", "/unknown", "foo /exit"),
)
def test_tty_unknown_slash_input_runs_agent_turn_unmodified(
    tmp_path: Path,
    monkeypatch,
    task: str,
) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self.closed = False
            self._responses = iter((task, ":q"))
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str:
            del prompt
            return next(self._responses)

        async def confirm(self, prompt: str) -> bool:
            del prompt
            raise AssertionError("confirmation was not expected")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Done."),
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    sessions = _install_session_capture(monkeypatch)
    stdout = StringIO()
    stderr = _TerminalOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=TerminalInput(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Done.\n"
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[-1] == UserMessage.text(task)
    provider.assert_exhausted()
    assert FakeTuiSession.instances[0].closed is True
    assert sessions == [sessions[0]]


def test_tty_slash_usage_shows_zero_before_any_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self._responses = iter(("/usage", "/exit"))
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str:
            del prompt
            return next(self._responses)

        async def confirm(self, prompt: str) -> bool:
            del prompt
            raise AssertionError("confirmation was not expected")

        async def close(self) -> None:
            pass

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)
    provider = ScriptedModelProvider(script=())
    sessions = _install_session_capture(monkeypatch)
    stdout = StringIO()
    stderr = _TerminalOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=TerminalInput(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "\033[2;32m[usage] input:0, output:0\033[0m\n" in stderr.getvalue()
    assert provider.requests == ()
    assert sessions == [sessions[0]]
    provider.assert_exhausted()


def test_tty_slash_usage_shows_cumulative_across_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self._responses = iter(("work", "/usage", "more", "/usage", "/exit"))
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str:
            del prompt
            return next(self._responses)

        async def confirm(self, prompt: str) -> bool:
            del prompt
            raise AssertionError("confirmation was not expected")

        async def close(self) -> None:
            pass

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Done."),
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                    usage=ModelUsage(
                        input_tokens=10,
                        output_tokens=20,
                        total_tokens=30,
                    ),
                ),
            ),
            (
                TextDelta(text="More."),
                ModelCompletion(
                    message=AssistantMessage.text("More."),
                    finish_reason="stop",
                    usage=ModelUsage(
                        input_tokens=3,
                        output_tokens=5,
                        total_tokens=8,
                    ),
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = _TerminalOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=TerminalInput(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Done.\nMore.\n"
    assert len(provider.requests) == 2
    usage_lines = [
        line
        for line in stderr.getvalue().splitlines()
        if "[usage]" in line
    ]
    assert usage_lines == [
        "\033[2;32m[usage] input:10, output:20\033[0m",
        "\033[2;32m[usage] input:13, output:25\033[0m",
    ]
    provider.assert_exhausted()


def test_tty_slash_usage_skips_completions_without_usage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self._responses = iter(("first", "/usage", "second", "/usage", "/exit"))
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str:
            del prompt
            return next(self._responses)

        async def confirm(self, prompt: str) -> bool:
            del prompt
            raise AssertionError("confirmation was not expected")

        async def close(self) -> None:
            pass

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Done."),
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                    usage=ModelUsage(
                        input_tokens=10,
                        output_tokens=20,
                        total_tokens=30,
                    ),
                ),
            ),
            (
                TextDelta(text="More."),
                ModelCompletion(
                    message=AssistantMessage.text("More."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = _TerminalOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=TerminalInput(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert len(provider.requests) == 2
    usage_lines = [
        line
        for line in stderr.getvalue().splitlines()
        if "[usage]" in line
    ]
    assert usage_lines == [
        "\033[2;32m[usage] input:10, output:20\033[0m",
        "\033[2;32m[usage] input:10, output:20\033[0m",
    ]
    provider.assert_exhausted()


def test_tty_slash_usage_accumulates_completions_within_one_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self._responses = iter(("work", "/usage", "/exit"))
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str:
            del prompt
            return next(self._responses)

        async def confirm(self, prompt: str) -> bool:
            del prompt
            raise AssertionError("confirmation was not expected")

        async def close(self) -> None:
            pass

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)
    call = ToolCall(
        call_id="usage_proof",
        name="exec",
        arguments={"command": _python_command("print('proof')")},
    )
    tool_message = AssistantMessage(content=(call,))
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                    usage=ModelUsage(
                        input_tokens=100,
                        output_tokens=10,
                        total_tokens=110,
                    ),
                ),
            ),
            (
                TextDelta(text="Done."),
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                    usage=ModelUsage(
                        input_tokens=50,
                        output_tokens=5,
                        total_tokens=55,
                    ),
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = _TerminalOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=TerminalInput(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Done.\n"
    assert len(provider.requests) == 2
    usage_lines = [
        line
        for line in stderr.getvalue().splitlines()
        if "[usage]" in line
    ]
    assert usage_lines == [
        "\033[2;32m[usage] input:150, output:15\033[0m",
    ]
    provider.assert_exhausted()


def test_non_tty_slash_usage_runs_as_regular_task(tmp_path: Path) -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Done."),
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=StringIO("/usage\n:q\n"),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Done.\n"
    assert "[usage]" not in stderr.getvalue()
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[-1] == UserMessage.text("/usage")
    provider.assert_exhausted()


def test_render_session_usage_is_plain_off_tty_and_styled_on_tty() -> None:
    plain = StringIO()
    render_session_usage(
        SessionUsage(input_tokens=1234, output_tokens=567),
        stderr=plain,
    )
    assert plain.getvalue() == "[usage] input:1234, output:567\n"

    tty = _TerminalOutput()
    render_session_usage(None, stderr=tty)
    assert tty.getvalue() == "\033[2;32m[usage] input:0, output:0\033[0m\n"


def test_one_shot_task_value_slash_exit_runs_as_regular_task(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Done."),
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task="/exit"),
            provider,
            stdin=StringIO(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Done."
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[-1] == UserMessage.text("/exit")
    provider.assert_exhausted()


def test_non_tty_slash_exit_runs_as_regular_task(tmp_path: Path) -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="Done."),
                ModelCompletion(
                    message=AssistantMessage.text("Done."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=StringIO("/exit\n:q\n"),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Done.\n"
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[-1] == UserMessage.text("/exit")
    provider.assert_exhausted()


def test_tty_session_reuses_one_input_session_for_task_and_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TtyInput(StringIO):
        def isatty(self) -> bool:
            return True

        def readline(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("TTY input must be owned by TuiSession")

    class FakeTuiSession:
        instances: list["FakeTuiSession"] = []

        def __init__(self, *, stdin, stderr, specs) -> None:
            del stdin, stderr, specs
            self.read_prompts: list[str] = []
            self.confirm_prompts: list[str] = []
            self.closed = False
            self._responses = iter(("Run task", ":q"))
            self.instances.append(self)

        async def read_text(self, prompt: str) -> str:
            self.read_prompts.append(prompt)
            return next(self._responses)

        async def confirm(self, prompt: str) -> bool:
            self.confirm_prompts.append(prompt)
            return True

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(runner_module, "TuiSession", FakeTuiSession)

    proof = tmp_path / "approval-proof.txt"
    proof.write_text("preserved", encoding="utf-8")
    command = "rm approval-proof.txt"
    call = ToolCall(
        call_id="review_rm_tty",
        name="exec",
        arguments={"command": command},
    )
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=call),
                ModelCompletion(
                    message=AssistantMessage(content=(call,)),
                    finish_reason="tool_calls",
                ),
            ),
            (
                TextDelta(text="Reviewed."),
                ModelCompletion(
                    message=AssistantMessage.text("Reviewed."),
                    finish_reason="stop",
                ),
            ),
        )
    )
    stdout = StringIO()
    stderr = _TerminalOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            execution_policy=_AskExecutablePolicy(
                frozenset({"rm"}),
                rule_id="test.ask-rm-tty",
                reason="direct invocation of 'rm' requires Host approval",
            ),
            stdin=TtyInput(),
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert exit_code == 0
    assert proof.exists() is False
    assert stdout.getvalue() == "Reviewed.\n"
    assert "cli-agent> " not in stderr.getvalue()
    assert "[interaction] direct invocation of 'rm' requires Host approval\n" in (
        stderr.getvalue()
    )
    tui_session = FakeTuiSession.instances[0]
    assert tui_session.read_prompts == ["cli-agent> ", "cli-agent> "]
    assert tui_session.confirm_prompts == ["Allow once? [y/N] "]
    assert tui_session.closed is True
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
    original_detach_session = AgentRuntime.detach_session
    original_close = AgentRuntime.close

    async def tracking_detach_session(runtime: AgentRuntime) -> None:
        lifecycle.append("session")
        await original_detach_session(runtime)

    async def tracking_close(runtime: AgentRuntime) -> None:
        lifecycle.append("runtime")
        await original_close(runtime)

    monkeypatch.setattr(AgentRuntime, "detach_session", tracking_detach_session)
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
    assert stderr.getvalue() == "[tool] exec: pwd\n[completion] reason=stop\n"


def test_renderer_keeps_non_exec_tool_diagnostic_concise() -> None:
    stderr = StringIO()

    render_event(
        ToolCallReady(
            call=ToolCall(
                call_id="call_1",
                name="output",
                arguments={"exec_id": "execution_1"},
            )
        ),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert stderr.getvalue() == "[tool] output\n"


def test_renderer_colors_terminal_tool_and_completion_diagnostics() -> None:
    stderr = _TerminalOutput()

    render_event(
        ToolCallReady(
            call=ToolCall(
                call_id="call_1",
                name="exec",
                arguments={"command": "pytest -q"},
            )
        ),
        stdout=StringIO(),
        stderr=stderr,
    )
    render_event(
        ModelCompletion(
            message=AssistantMessage.text("Done"),
            finish_reason="stop",
        ),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert stderr.getvalue() == (
        "\033[1;35m[tool] exec\033[0m: \033[33mpytest -q\033[0m\n"
        "\033[2;32m[completion] reason=stop\033[0m\n"
    )


def test_renderer_presents_a_runtime_diagnostic() -> None:
    stderr = StringIO()

    render_diagnostic(
        RuntimeDiagnostic(
            kind="mcp.discovery_failed",
            message="could not contact github",
        ),
        stderr=stderr,
    )

    assert stderr.getvalue() == ("[mcp.discovery_failed] could not contact github\n")


def test_renderer_presents_context_diagnostics_without_detail() -> None:
    stderr = StringIO()

    render_diagnostic(
        RuntimeDiagnostic(
            kind="context.snipped",
            message="context compaction released 47120 projected input tokens",
            detail={
                "session_id": "session-1",
                "revision_before": 3,
                "revision_after": 4,
            },
        ),
        stderr=stderr,
    )

    assert stderr.getvalue() == (
        "[context.snipped] context compaction released 47120 projected input tokens\n"
    )
    assert "session-1" not in stderr.getvalue()


class _TerminalOutput(StringIO):
    def isatty(self) -> bool:
        return True


def _install_session_capture(monkeypatch) -> list[str]:
    sessions: list[str] = []
    original_new_session = AgentRuntime.new_session

    async def tracking_new_session(
        runtime: AgentRuntime,
        *,
        provider: object = None,
    ) -> object:
        session = await original_new_session(runtime, provider=provider)
        sessions.append(session.session_id)
        return session

    monkeypatch.setattr(AgentRuntime, "new_session", tracking_new_session)
    return sessions


def _config(
    tmp_path: Path,
    *,
    task: str | None = "Run the task",
    repertoire: Path | None = None,
) -> CliConfig:
    return CliConfig(
        task=task,
        workspace=tmp_path,
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        repertoire=repertoire,
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=4_096,
    )


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
