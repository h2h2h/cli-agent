import asyncio
import shlex
import sys
from pathlib import Path

from runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelRequest,
    ScriptedModelProvider,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResultMessage,
    UserMessage,
)
from runtime._agent_loop import AgentLoop
from runtime._environment import EnvironmentKernel


def test_completes_a_text_only_turn(tmp_path: Path) -> None:
    user_message = UserMessage.text("Hello")
    assistant_message = AssistantMessage.text("Hi")
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="H"),
                TextDelta(text="i"),
                ModelCompletion(
                    message=assistant_message,
                    finish_reason="stop",
                ),
            ),
        )
    )
    loop = AgentLoop(provider, EnvironmentKernel(tmp_path).create_binding())

    events = asyncio.run(_collect_events(loop, user_message))

    assert provider.requests == (ModelRequest(messages=(user_message,)),)
    assert events == (
        TextDelta(text="H"),
        TextDelta(text="i"),
        ModelCompletion(
            message=assistant_message,
            finish_reason="stop",
        ),
    )
    assert loop.history == (user_message, assistant_message)
    provider.assert_exhausted()


def test_continues_generation_after_exec_tool_result(tmp_path: Path) -> None:
    user_message = UserMessage.text("Inspect the workspace")
    call = ToolCall(
        call_id="call_1",
        name="exec",
        arguments={"command": _python_command("print('workspace inspected')")},
    )
    tool_message = AssistantMessage(
        content=(TextBlock(text="I will inspect it."), call),
    )
    final_message = AssistantMessage.text("Inspection complete.")
    provider = ScriptedModelProvider(
        script=(
            (
                TextDelta(text="I will inspect it."),
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
                ),
            ),
        )
    )
    kernel = EnvironmentKernel(tmp_path)
    loop = AgentLoop(provider, kernel.create_binding())

    events = asyncio.run(_collect_events(loop, user_message))

    assert events == (
        TextDelta(text="I will inspect it."),
        ToolCallReady(call=call),
        TextDelta(text="Inspection complete."),
        ModelCompletion(message=final_message, finish_reason="stop"),
    )
    assert len(provider.requests) == 2
    first_request, second_request = provider.requests
    assert first_request.messages == (user_message,)
    for request in provider.requests:
        assert tuple(tool.name for tool in request.tools) == (
            "exec",
            "output",
            "kill",
        )

    assert second_request.messages[:2] == (user_message, tool_message)
    result_message = second_request.messages[2]
    assert isinstance(result_message, ToolResultMessage)
    result = result_message.content[0]
    assert result.call_id == call.call_id
    assert result.error is None
    assert isinstance(result.output, dict)
    assert result.output["ok"] is True
    assert result.output["status"] == "exited"
    assert result.output["exit_code"] == 0
    chunks = result.output["chunks"]
    assert isinstance(chunks, list)
    assert "workspace inspected\n" in "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == "stdout"
    )
    assert loop.history == (
        user_message,
        tool_message,
        result_message,
        final_message,
    )
    provider.assert_exhausted()


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


async def _collect_events(
    loop: AgentLoop,
    user_message: UserMessage,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in loop.run(user_message)])
