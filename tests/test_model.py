import asyncio
from collections.abc import AsyncIterator

import pytest

from runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    ModelUsage,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


def test_constructs_a_provider_neutral_text_conversation() -> None:
    system_message = SystemMessage.text("Follow the Runtime instructions.")
    user_message = UserMessage.text("Hello")
    assistant_message = AssistantMessage.text("Hi")

    request = ModelRequest(
        messages=(system_message, user_message, assistant_message),
    )

    assert system_message == SystemMessage(
        content=(TextBlock(text="Follow the Runtime instructions."),),
    )
    assert user_message == UserMessage(content=(TextBlock(text="Hello"),))
    assert assistant_message == AssistantMessage(content=(TextBlock(text="Hi"),))
    assert request.messages == (system_message, user_message, assistant_message)


def test_represents_streamed_text_and_terminal_completion() -> None:
    assistant_message = AssistantMessage.text("Hi")

    events = (
        TextDelta(text="H"),
        TextDelta(text="i"),
        ModelCompletion(message=assistant_message, finish_reason="stop"),
    )

    assert events[-1].message == assistant_message
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage is None


def test_represents_provider_neutral_model_usage() -> None:
    usage = ModelUsage(
        input_tokens=12,
        output_tokens=5,
        total_tokens=17,
    )

    completion = ModelCompletion(
        message=AssistantMessage.text("Done"),
        finish_reason="stop",
        usage=usage,
    )

    assert completion.usage == usage
    assert completion == ModelCompletion(
        message=AssistantMessage.text("Done"),
        finish_reason="stop",
        usage=ModelUsage(
            input_tokens=12,
            output_tokens=5,
            total_tokens=17,
        ),
    )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "total_tokens"),
    ((-1, 0, 0), (0, -1, 0), (0, 0, -1)),
)
def test_rejects_negative_model_usage(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> None:
    with pytest.raises(ValueError, match="must be non-negative"):
        ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


def test_provider_exposes_an_asynchronous_model_event_stream() -> None:
    request = ModelRequest(messages=(UserMessage.text("Hello"),))

    events = asyncio.run(_collect_events(GreetingProvider(), request))

    assert events == (
        TextDelta(text="Hi"),
        ModelCompletion(
            message=AssistantMessage.text("Hi"),
            finish_reason="stop",
        ),
    )


def test_represents_a_tool_call_with_decoded_arguments() -> None:
    call = ToolCall(
        call_id="call_1",
        name="exec",
        arguments={"cmd": "ls -la", "timeout_ms": 5000},
    )

    assert call == ToolCall(
        call_id="call_1",
        name="exec",
        arguments={"cmd": "ls -la", "timeout_ms": 5000},
    )
    assert call.arguments["cmd"] == "ls -la"


def test_assistant_message_retains_text_and_tool_call_order() -> None:
    opening = TextBlock(text="Let me check.")
    call = ToolCall(call_id="call_1", name="exec", arguments={"cmd": "ls"})
    closing = TextBlock(text="Done.")

    message = AssistantMessage(content=(opening, call, closing))

    assert message.content == (opening, call, closing)


def test_tool_result_round_trips_through_history() -> None:
    user = UserMessage.text("list files")
    call = ToolCall(call_id="call_1", name="exec", arguments={"cmd": "ls"})
    assistant = AssistantMessage(content=(call,))
    result = ToolResult(
        call_id="call_1",
        output={"exit_code": 0, "stdout": "file.txt"},
    )
    tool_message = ToolResultMessage(content=(result,))

    request = ModelRequest(messages=(user, assistant, tool_message))

    assert request.messages == (user, assistant, tool_message)
    assert request.messages[1].content[0] is call
    assert request.messages[2].content[0] is result
    assert request.messages[2].content[0].call_id == "call_1"


def test_tool_result_distinguishes_success_and_failure() -> None:
    success = ToolResult(call_id="c1", output={"exit_code": 0})
    failure = ToolResult(call_id="c2", error={"message": "command not found"})

    assert success.output == {"exit_code": 0}
    assert success.error is None
    assert failure.error == {"message": "command not found"}
    assert failure.output is None


def test_tool_call_ready_event_carries_a_call() -> None:
    call = ToolCall(call_id="call_1", name="exec", arguments={"cmd": "ls"})

    event = ToolCallReady(call=call)

    assert event.call == call


class GreetingProvider:
    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        assert request.messages == (UserMessage.text("Hello"),)
        yield TextDelta(text="Hi")
        yield ModelCompletion(
            message=AssistantMessage.text("Hi"),
            finish_reason="stop",
        )


async def _collect_events(
    provider: ModelProvider,
    request: ModelRequest,
) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in provider.generate(request)])
