import asyncio
import shlex
import sys
from pathlib import Path

import pytest
from host_fakes import _environment_kernel
from interaction_fakes import (
    _BlockingInteraction,
    _InvalidAnswerInteraction,
    _ScriptedInteraction,
)
from policy_fakes import _AskExecutablePolicy
from workspace_fakes import _kernel_workspace

from cli_agent.runtime import (
    ToolCall,
    ToolResult,
    UserAnswer,
    UserOption,
    UserQuestion,
)


def test_ask_standard_question_exposes_reason_command_and_fixed_options(
    tmp_path: Path,
) -> None:
    interaction = _ScriptedInteraction("allow_once")
    proof = tmp_path / "approved"
    command = _python_command(f"from pathlib import Path; Path({str(proof)!r}).touch()")

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
            interaction=interaction,
            session_id="session-a",
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="approved",
                    name="exec",
                    arguments={"command": command},
                )
            )

            snapshot = _output(result)
            assert snapshot["status"] == "exited"
            assert proof.exists()
            assert len(interaction.questions) == 1
            question = interaction.questions[0]
            assert isinstance(question, UserQuestion)
            assert question.request_id
            assert question.session_id == "session-a"
            assert question.prompt == (
                "direct invocation of python requires Host approval\n"
                f"command: {command}"
            )
            assert question.options == (
                UserOption(value="allow_once", label="Allow once"),
                UserOption(value="deny", label="Deny"),
            )
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_allow_once_executes_once_and_does_not_persist(tmp_path: Path) -> None:
    interaction = _ScriptedInteraction("allow_once")

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
            interaction=interaction,
        )
        try:
            first = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id="first-approval",
                        name="exec",
                        arguments={"command": _python_command("pass")},
                    )
                )
            )
            second = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id="second-approval",
                        name="exec",
                        arguments={"command": _python_command("pass")},
                    )
                )
            )

            assert first["status"] == "exited"
            assert second["status"] == "exited"
            assert len(interaction.questions) == 2
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_ask_deny_blocks_command_with_policy_reason(tmp_path: Path) -> None:
    interaction = _ScriptedInteraction("deny")
    proof = tmp_path / "denied"

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
            interaction=interaction,
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="denied",
                    name="exec",
                    arguments={"command": _touch_command(proof)},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": "direct invocation of python requires Host approval",
            }
            assert not proof.exists()
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_ask_none_fails_closed_with_generic_message(tmp_path: Path) -> None:
    interaction = _ScriptedInteraction(None)
    proof = tmp_path / "cancelled"

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
            interaction=interaction,
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="cancelled-ask",
                    name="exec",
                    arguments={"command": _touch_command(proof)},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": "execution was not approved by the user",
            }
            assert not proof.exists()
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_ask_without_interaction_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="no-interaction",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": "direct invocation of python requires Host approval",
            }
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_interaction_exception_fails_closed_with_diagnostic(tmp_path: Path) -> None:
    class FailsOnceInteraction:
        def __init__(self) -> None:
            self._fail = True

        async def ask(self, request: UserQuestion) -> UserAnswer:
            if self._fail:
                self._fail = False
                raise RuntimeError(request.request_id)
            return UserAnswer(value="allow_once")

    diagnostics: list[object] = []

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
            interaction=FailsOnceInteraction(),  # type: ignore[arg-type]
            events=diagnostics.append,
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="failing-interaction",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": "execution interaction failed closed",
            }
            assert kernel._executions == {}

            still_usable = await kernel.dispatch(
                ToolCall(
                    call_id="session-still-usable",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )
            assert _output(still_usable)["status"] == "exited"
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert [d.kind for d in diagnostics] == ["execution_interaction.failed"]
    assert "RuntimeError" in diagnostics[0].detail["exception"]


@pytest.mark.parametrize(
    "interaction",
    (
        _InvalidAnswerInteraction(),
        _ScriptedInteraction("undeclared-option"),
    ),
)
def test_invalid_interaction_answer_fails_closed_with_diagnostic(
    tmp_path: Path,
    interaction: object,
) -> None:
    diagnostics: list[object] = []

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
            interaction=interaction,  # type: ignore[arg-type]
            events=diagnostics.append,
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="invalid-answer",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": "execution interaction failed closed",
            }
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert [d.kind for d in diagnostics] == ["execution_interaction.invalid_answer"]


def test_session_close_cancels_pending_ask_without_closing_interaction(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        interaction = _BlockingInteraction()
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            policy=_ask_for_python_policy(),
            interaction=interaction,
        )
        dispatch = asyncio.create_task(
            kernel.dispatch(
                ToolCall(
                    call_id="cancelled-ask",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )
        )

        await interaction.entered.wait()
        assert kernel._executions == {}
        await kernel.close()
        result = await asyncio.wait_for(dispatch, timeout=0.5)

        assert _error(result) == {
            "ok": False,
            "code": "internal",
            "message": "environment session is closed",
        }
        assert interaction.cancelled.is_set()
        assert interaction.closed is False
        assert kernel._executions == {}

    asyncio.run(scenario())


def _ask_for_python_policy() -> _AskExecutablePolicy:
    return _AskExecutablePolicy(
        frozenset({Path(sys.executable).name}),
        rule_id="shell.ask-executable.python",
        reason="direct invocation of python requires Host approval",
    )


def _touch_command(path: Path) -> str:
    return _python_command(f"from pathlib import Path; Path({str(path)!r}).touch()")


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _error(result: ToolResult) -> dict[str, object]:
    assert result.output is None
    assert isinstance(result.error, dict)
    return result.error
