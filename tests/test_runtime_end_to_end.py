import asyncio
import shlex
import socket
import sys
from pathlib import Path

from runtime import (
    AgentRuntime,
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


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
        async with AgentRuntime.open(
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
            assert first_request.messages == (first_user,)
            result_message = result_request.messages[2]
            assert isinstance(result_message, ToolResultMessage)
            assert result_request.messages == (
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
                first_user,
                tool_message,
                result_message,
                final_message,
                history_user,
            )

            await runtime.close_session("session-a")
            await _collect_turn(runtime, "session-a", fresh_user)

            assert provider.requests[3].messages == (fresh_user,)
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
