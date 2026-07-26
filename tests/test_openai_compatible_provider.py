import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    OpenAICompatibleModelProvider,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


def test_streams_a_real_model_request_through_the_provider_neutral_seam() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"role":"assistant"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"content":"Hel"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"content":"lo"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1/",
        transport=httpx.MockTransport(respond),
    )
    model_request = ModelRequest(
        messages=(
            SystemMessage.text("Be concise."),
            UserMessage.text("Hello"),
            AssistantMessage.text("How can I help?"),
            UserMessage.text("Reply briefly."),
        )
    )

    events = asyncio.run(_collect_events(provider, model_request))

    assert len(requests) == 1
    assert requests[0].url == "https://models.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer secret-key"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "test-model"
    assert payload["messages"] == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "How can I help?"},
        {"role": "user", "content": "Reply briefly."},
    ]
    assert payload["stream"] is True
    assert "tool_choice" not in payload
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in model_request.tools
    ]
    assert [tool_payload["function"]["name"] for tool_payload in payload["tools"]] == [
        "exec",
        "output",
        "kill",
    ]
    assert all(
        "output_schema" not in tool_payload["function"]
        for tool_payload in payload["tools"]
    )
    assert events == (
        TextDelta(text="Hel"),
        TextDelta(text="lo"),
        ModelCompletion(
            message=AssistantMessage.text("Hello"),
            finish_reason="stop",
        ),
    )


def test_encodes_tool_calls_and_expands_ordered_tool_results() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    first_call = ToolCall(
        call_id="call_exec",
        name="exec",
        arguments={"command": "pwd"},
    )
    second_call = ToolCall(
        call_id="call_output",
        name="output",
        arguments={"exec_id": "exec_1", "cursor": 0},
    )
    assistant_message = AssistantMessage(
        content=(
            TextBlock(text="Running. "),
            first_call,
            TextBlock(text="Checking output."),
            second_call,
        ),
    )
    successful_result = ToolResult(
        call_id=first_call.call_id,
        output={"ok": True, "exec_id": "exec_1"},
    )
    failed_result = ToolResult(
        call_id=second_call.call_id,
        error={
            "ok": False,
            "code": "unknown_execution",
            "message": "execution not found",
        },
    )
    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1",
        transport=httpx.MockTransport(respond),
    )
    model_request = ModelRequest(
        messages=(
            SystemMessage.text("Use the available tools."),
            UserMessage.text("Inspect the Workspace."),
            assistant_message,
            ToolResultMessage(content=(successful_result, failed_result)),
            UserMessage.text("Continue."),
        )
    )

    asyncio.run(_collect_events(provider, model_request))

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["messages"] == [
        {"role": "system", "content": "Use the available tools."},
        {"role": "user", "content": "Inspect the Workspace."},
        {
            "role": "assistant",
            "content": "Running. Checking output.",
            "tool_calls": [
                {
                    "id": first_call.call_id,
                    "type": "function",
                    "function": {
                        "name": first_call.name,
                        "arguments": json.dumps(first_call.arguments),
                    },
                },
                {
                    "id": second_call.call_id,
                    "type": "function",
                    "function": {
                        "name": second_call.name,
                        "arguments": json.dumps(second_call.arguments),
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": successful_result.call_id,
            "content": json.dumps(successful_result.output),
        },
        {
            "role": "tool",
            "tool_call_id": failed_result.call_id,
            "content": json.dumps(failed_result.error),
        },
        {"role": "user", "content": "Continue."},
    ]


async def _collect_events(
    provider: ModelProvider,
    request: ModelRequest,
) -> tuple[ModelEvent, ...]:
    events: AsyncIterator[ModelEvent] = provider.generate(request)
    return tuple([event async for event in events])
