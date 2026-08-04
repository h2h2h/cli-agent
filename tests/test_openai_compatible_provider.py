import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelContextOverflowError,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    ModelUsage,
    OpenAICompatibleModelProvider,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolCallReady,
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
                'data: {"choices":[],"usage":{"prompt_tokens":11,'
                '"completion_tokens":2,"total_tokens":13}}\n\n'
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
    assert payload["stream_options"] == {"include_usage": True}
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
            usage=ModelUsage(
                input_tokens=11,
                output_tokens=2,
                total_tokens=13,
            ),
        ),
    )


def test_omits_tools_field_for_internal_summary_requests() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"content":"Summary"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1",
        transport=httpx.MockTransport(respond),
    )
    request = ModelRequest(
        messages=(UserMessage.text("Summarize"),),
        tools=(),
    )

    events = asyncio.run(_collect_events(provider, request))

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert "tools" not in payload
    assert events == (
        TextDelta(text="Summary"),
        ModelCompletion(
            message=AssistantMessage.text("Summary"),
            finish_reason="stop",
            usage=None,
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

    events = asyncio.run(_collect_events(provider, model_request))

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
    assert events == (
        ModelCompletion(
            message=AssistantMessage.text(""),
            finish_reason="stop",
            usage=None,
        ),
    )


def test_assembles_fragmented_tool_calls_in_index_order_with_usage() -> None:
    response_text = _sse(
        {
            "choices": [
                {
                    "delta": {"content": "Checking. "},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_output",
                                "function": {
                                    "name": "output",
                                    "arguments": '{"exec',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_exec",
                                "function": {
                                    "name": "exec",
                                    "arguments": '{"command":',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "function": {
                                    "arguments": '_id":"exec_1"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"pwd"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 12,
                "total_tokens": 52,
            },
        },
    )
    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=response_text,
            )
        ),
    )
    request = ModelRequest(messages=(UserMessage.text("Inspect."),))
    exec_call = ToolCall(
        call_id="call_exec",
        name="exec",
        arguments={"command": "pwd"},
    )
    output_call = ToolCall(
        call_id="call_output",
        name="output",
        arguments={"exec_id": "exec_1"},
    )
    usage = ModelUsage(
        input_tokens=40,
        output_tokens=12,
        total_tokens=52,
    )

    events = asyncio.run(_collect_events(provider, request))

    assert events == (
        TextDelta(text="Checking. "),
        ToolCallReady(call=exec_call),
        ToolCallReady(call=output_call),
        ModelCompletion(
            message=AssistantMessage(
                content=(
                    TextBlock(text="Checking. "),
                    exec_call,
                    output_call,
                )
            ),
            finish_reason="tool_calls",
            usage=usage,
        ),
    )


@pytest.mark.parametrize(
    ("tool_delta", "error_match"),
    (
        (
            {"index": 0, "function": {"name": "exec", "arguments": "{}"}},
            "missing id",
        ),
        (
            {"index": 0, "id": "call_1", "function": {"arguments": "{}"}},
            "missing function name",
        ),
        (
            {
                "index": 0,
                "id": "call_1",
                "function": {"name": "exec", "arguments": "{"},
            },
            "invalid argument JSON",
        ),
        (
            {
                "index": 0,
                "id": "call_1",
                "function": {"name": "exec", "arguments": "[]"},
            },
            "arguments must be a JSON object",
        ),
    ),
)
def test_rejects_incomplete_tool_calls_without_ready_events(
    tool_delta: dict[str, object],
    error_match: str,
) -> None:
    response_text = _sse(
        {
            "choices": [
                {
                    "delta": {"content": "Preparing."},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [tool_delta]},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=response_text,
            )
        ),
    )
    events: list[ModelEvent] = []

    async def consume() -> None:
        async for event in provider.generate(
            ModelRequest(messages=(UserMessage.text("Inspect."),))
        ):
            events.append(event)

    with pytest.raises(ValueError, match=error_match):
        asyncio.run(consume())

    assert events == [TextDelta(text="Preparing.")]


def _sse(*chunks: dict[str, object]) -> str:
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )


def _error_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        400,
        json=payload,
    )


def _collect_error(provider: OpenAICompatibleModelProvider) -> None:
    async def consume() -> None:
        async for _event in provider.generate(
            ModelRequest(messages=(UserMessage.text("Hello"),))
        ):
            pass

    asyncio.run(consume())


@pytest.mark.parametrize(
    "payload",
    (
        {
            "error": {
                "message": (
                    "This model's maximum context length is 8192 tokens; "
                    "you requested 20000 tokens."
                ),
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
            }
        },
        {
            "error": {
                "message": "too many tokens",
                "type": "context_length_exceeded",
            }
        },
        {"error": {"code": "ContextWindowExceeded"}},
        {
            "error": {
                "message": "request exceeds the maximum context window of 128K tokens"
            }
        },
        {"message": "prompt is too long"},
    ),
)
def test_maps_structured_context_overflow_errors(payload: dict[str, object]) -> None:
    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1",
        transport=httpx.MockTransport(lambda request: _error_response(payload)),
    )

    with pytest.raises(ModelContextOverflowError):
        _collect_error(provider)


@pytest.mark.parametrize(
    ("status", "payload"),
    (
        (401, {"error": {"message": "Incorrect API key", "code": "invalid_api_key"}}),
        (
            429,
            {
                "error": {
                    "message": "Rate limit reached",
                    "type": "rate_limit_error",
                }
            },
        ),
        (500, {"error": {"message": "server exploded"}}),
        (400, {"error": {"message": "invalid request body"}}),
        (400, "not a json object"),
        (502, {"error": {"message": "bad gateway", "code": "bad_gateway"}}),
    ),
)
def test_keeps_non_overflow_errors_unmapped(
    status: int,
    payload: object,
) -> None:
    response = (
        httpx.Response(status, json=payload)
        if isinstance(payload, dict)
        else httpx.Response(status, content=b"not a json object")
    )
    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="secret-key",
        base_url="https://models.example/v1",
        transport=httpx.MockTransport(lambda request: response),
    )

    with pytest.raises(httpx.HTTPStatusError):
        _collect_error(provider)


async def _collect_events(
    provider: ModelProvider,
    request: ModelRequest,
) -> tuple[ModelEvent, ...]:
    events: AsyncIterator[ModelEvent] = provider.generate(request)
    return tuple([event async for event in events])
