import asyncio
import shlex
import socket
import sys
from pathlib import Path

from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime as runtime_package
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)

_user_interaction = _ScriptedInteraction("allow_once")



def test_runs_the_smallest_deterministic_agent_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)

    first_user = UserMessage.text("Create and inspect a proof file")
    write_call, read_call = _ordered_file_calls()
    tool_message = AssistantMessage(
        content=(
            TextBlock(text="I will create and inspect it."),
            write_call,
            read_call,
        )
    )
    final_message = AssistantMessage.text("The proof file contains: written-first")
    history_user = UserMessage.text("Confirm what happened")
    history_message = AssistantMessage.text("The ordered execution is in history.")
    fresh_user = UserMessage.text("Start fresh")
    fresh_message = AssistantMessage.text("This is a fresh Session.")
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=write_call),
                ToolCallReady(call=read_call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            ),
            (
                TextDelta(text="The proof file contains: written-first"),
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            ),
            (
                ModelCompletion(
                    message=history_message,
                    finish_reason="stop",
                ),
            ),
            (
                ModelCompletion(
                    message=fresh_message,
                    finish_reason="stop",
                ),
            ),
        )
    )

    async def scenario() -> None:
        async with await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
        ) as runtime:
            first_events = await _collect_turn(
                runtime,
                "session-a",
                first_user,
            )

            assert first_events == (
                ToolCallReady(call=write_call),
                ToolCallReady(call=read_call),
                TextDelta(text="The proof file contains: written-first"),
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            )
            assert (tmp_path / "proof.txt").read_text() == "written-first"

            first_request, result_request = provider.requests[:2]
            system_message = first_request.messages[0]
            assert isinstance(system_message, SystemMessage)
            assert first_request.messages == (system_message, first_user)
            result_message = result_request.messages[3]
            assert isinstance(result_message, ToolResultMessage)
            assert result_request.messages == (
                system_message,
                first_user,
                tool_message,
                result_message,
            )
            assert tuple(result.call_id for result in result_message.content) == (
                write_call.call_id,
                read_call.call_id,
            )
            assert _execution_status(result_message.content[0]) == "exited"
            assert _execution_status(result_message.content[1]) == "exited"
            assert _stdout(result_message.content[1]) == "written-first\n"

            await _collect_turn(runtime, "session-a", history_user)

            assert provider.requests[2].messages == (
                system_message,
                first_user,
                tool_message,
                result_message,
                final_message,
                history_user,
            )

            await runtime.close_session("session-a")
            await _collect_turn(runtime, "session-a", fresh_user)

            fresh_system_message = provider.requests[3].messages[0]
            assert isinstance(fresh_system_message, SystemMessage)
            assert fresh_system_message is not system_message
            assert provider.requests[3].messages == (
                fresh_system_message,
                fresh_user,
            )
            await runtime.close_session("session-a")
            await runtime.close_session("session-a")
            assert not runtime.closed

        assert runtime.closed
        for request in provider.requests:
            assert tuple(tool.name for tool in request.tools) == (
                "exec",
                "output",
                "kill",
            )
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_skill_is_discoverable_and_loaded_on_demand(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)

    repertoire = _skill_repertoire(tmp_path)
    _write_skill(
        repertoire,
        "banner-skill",
        "Add a proof banner.",
        "# Banner skill\n\nRun `print('BANNER')` to add a proof banner.\n",
    )

    read_call = ToolCall(
        call_id="read_banner",
        name="exec",
        arguments={"command": "cat .workspace/skills/banner-skill/SKILL.md"},
    )
    tool_message = AssistantMessage(
        content=(
            TextBlock(text="I will read the banner skill."),
            read_call,
        )
    )
    final_message = AssistantMessage.text("The banner skill was loaded on demand.")
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=read_call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            ),
        )
    )

    async def scenario() -> None:
        async with await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            repertoire=repertoire,
        ) as runtime:
            events = await _collect_turn(
                runtime,
                "session-skill",
                UserMessage.text("Load the banner skill"),
            )
            assert events == (
                ToolCallReady(call=read_call),
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            )

            first_request = provider.requests[0]
            system_message = first_request.messages[0]
            assert isinstance(system_message, SystemMessage)
            system_body = "\n".join(block.text for block in system_message.content)
            assert "banner-skill (valid): Add a proof banner." in system_body
            assert "name: banner-skill" not in system_body
            assert "# Banner skill" not in system_body

            result_message = provider.requests[1].messages[3]
            assert isinstance(result_message, ToolResultMessage)
            assert _stdout(result_message.content[0]) == _skill_full(
                "banner-skill",
                "Add a proof banner.",
                "# Banner skill\n\nRun `print('BANNER')` to add a proof banner.\n",
            )

            await runtime.close_session("session-skill")

        assert runtime.closed
        for request in provider.requests:
            assert tuple(tool.name for tool in request.tools) == (
                "exec",
                "output",
                "kill",
            )
        provider.assert_exhausted()

    asyncio.run(scenario())

    _write_skill(repertoire, "second-skill", "Second proof skill.")

    provider = ScriptedModelProvider(
        script=(
            (
                ModelCompletion(
                    message=AssistantMessage.text("Ready."),
                    finish_reason="stop",
                ),
            ),
        )
    )

    async def second_scenario() -> None:
        async with await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            repertoire=repertoire,
        ) as runtime:
            await _collect_turn(
                runtime,
                "session-skills",
                UserMessage.text("List skills"),
            )

            system_message = provider.requests[0].messages[0]
            assert isinstance(system_message, SystemMessage)
            system_body = "\n".join(block.text for block in system_message.content)
            assert "banner-skill (valid): Add a proof banner." in system_body
            assert "second-skill (valid): Second proof skill." in system_body

        for request in provider.requests:
            assert tuple(tool.name for tool in request.tools) == (
                "exec",
                "output",
                "kill",
            )
        provider.assert_exhausted()

    asyncio.run(second_scenario())

    assert runtime_package.__all__ == (
        "AgentRuntime",
        "AssistantMessage",
        "ShellParseResult",
        "ExecutionPolicy",
        "JSONValue",
        "ModelCompletion",
        "ModelEvent",
        "ModelMessage",
        "ModelProvider",
        "ModelRequest",
        "ModelUsage",
        "OpenAICompatibleModelProvider",
        "PolicyAction",
        "PolicyEvaluation",
        "RuntimeClosedError",
        "RuntimeDiagnostic",
        "ScriptedModelProvider",
        "SystemMessage",
        "SyscallSchema",
        "TextBlock",
        "TextDelta",
        "ToolCall",
        "ToolCallReady",
        "ToolResult",
        "ToolResultMessage",
        "UserAnswer",
        "UserInteraction",
        "UserMessage",
        "UserOption",
        "UserQuestion",
    )


def _ordered_file_calls() -> tuple[ToolCall, ToolCall]:
    write_call = ToolCall(
        call_id="write_proof",
        name="exec",
        arguments={
            "command": _python_command(
                "from pathlib import Path; "
                "Path('proof.txt').write_text('written-first')"
            )
        },
    )
    read_call = ToolCall(
        call_id="read_proof",
        name="exec",
        arguments={
            "command": _python_command(
                "from pathlib import Path; print(Path('proof.txt').read_text())"
            )
        },
    )
    return write_call, read_call


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _skill_repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def _skill_full(name: str, description: str, body: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n" + body


def _write_skill(
    repertoire: Path,
    name: str,
    description: str,
    body: str = "",
) -> None:
    skill_directory = repertoire / "skills" / name
    skill_directory.mkdir(parents=True, exist_ok=True)
    (skill_directory / "SKILL.md").write_text(
        _skill_full(name, description, body),
        encoding="utf-8",
    )


def _execution_status(result: ToolResult) -> object:
    assert isinstance(result.output, dict)
    return result.output["status"]


def _stdout(result: ToolResult) -> str:
    assert isinstance(result.output, dict)
    chunks = result.output["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == "stdout"
    )


async def _collect_turn(
    runtime: AgentRuntime,
    session_id: str,
    message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(
                session_id,
                message,
            )
        ]
    )


def _deny_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("network access is forbidden in this scenario")
