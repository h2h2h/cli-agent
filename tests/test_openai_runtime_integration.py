import asyncio
import json
import shlex
import socket
import sys
from pathlib import Path

import httpx

from runtime import (
    AgentRuntime,
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelUsage,
    OpenAICompatibleModelProvider,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UserMessage,
)


def test_runs_an_openai_compatible_tool_round_trip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)

    command = _python_command(
        "from pathlib import Path; "
        "Path('round-trip.txt').write_text('from-provider'); "
        "print(Path('round-trip.txt').read_text())"
    )
    encoded_arguments = json.dumps({"command": command})
    split_at = len(encoded_arguments) // 2
    call = ToolCall(
        call_id="call_exec",
        name="exec",
        arguments={"command": command},
    )
    final_text = "Command output: from-provider"
    final_usage = ModelUsage(
        input_tokens=31,
        output_tokens=6,
        total_tokens=37,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response_number = len(requests)
        if response_number == 1:
            return _stream_response(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": call.call_id,
                                        "type": "function",
                                        "function": {
                                            "name": call.name,
                                            "arguments": encoded_arguments[:split_at],
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
                                        "function": {
                                            "arguments": encoded_arguments[split_at:],
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
                            "delta": {},
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        if response_number == 2:
            return _stream_response(
                {
                    "choices": [
                        {
                            "delta": {"content": "Command output: "},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {"content": "from-provider"},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": final_usage.input_tokens,
                        "completion_tokens": final_usage.output_tokens,
                        "total_tokens": final_usage.total_tokens,
                    },
                },
            )
        if response_number == 3:
            return _stream_response(
                {
                    "choices": [
                        {
                            "delta": {"content": "History confirmed."},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        raise AssertionError("unexpected model request")

    provider = OpenAICompatibleModelProvider(
        model="test-model",
        api_key="placeholder-key",
        base_url="https://models.invalid/v1",
        transport=httpx.MockTransport(respond),
    )
    user_message = UserMessage.text("Create the proof file.")
    follow_up = UserMessage.text("Confirm the previous result is in History.")

    async def scenario() -> None:
        async with AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
        ) as runtime:
            events = await _collect_turn(runtime, "session-a", user_message)
            follow_up_events = await _collect_turn(
                runtime,
                "session-a",
                follow_up,
            )

        assert runtime.closed
        assert events == (
            ToolCallReady(call=call),
            TextDelta(text="Command output: "),
            TextDelta(text="from-provider"),
            ModelCompletion(
                message=AssistantMessage.text(final_text),
                finish_reason="stop",
                usage=final_usage,
            ),
        )
        assert follow_up_events == (
            TextDelta(text="History confirmed."),
            ModelCompletion(
                message=AssistantMessage.text("History confirmed."),
                finish_reason="stop",
            ),
        )

    asyncio.run(scenario())

    assert len(requests) == 3
    payloads = [json.loads(request.content) for request in requests]
    first_payload, second_payload, history_payload = payloads

    assert all(
        request.headers["authorization"] == "Bearer placeholder-key"
        for request in requests
    )
    assert first_payload["messages"][0]["role"] == "system"
    assert str(tmp_path.resolve()) in first_payload["messages"][0]["content"]
    assert first_payload["messages"][1] == {
        "role": "user",
        "content": "Create the proof file.",
    }
    assert [tool["function"]["name"] for tool in first_payload["tools"]] == [
        "exec",
        "output",
        "kill",
    ]
    assert first_payload["stream_options"] == {"include_usage": True}

    assert second_payload["messages"][:2] == first_payload["messages"]
    assistant_payload = second_payload["messages"][2]
    assert assistant_payload["role"] == "assistant"
    assert assistant_payload["content"] == ""
    assert len(assistant_payload["tool_calls"]) == 1
    tool_call_payload = assistant_payload["tool_calls"][0]
    assert tool_call_payload["id"] == call.call_id
    assert tool_call_payload["function"]["name"] == call.name
    assert json.loads(tool_call_payload["function"]["arguments"]) == call.arguments

    tool_result_payload = second_payload["messages"][3]
    assert tool_result_payload["role"] == "tool"
    assert tool_result_payload["tool_call_id"] == call.call_id
    tool_result = json.loads(tool_result_payload["content"])
    assert tool_result["ok"] is True
    assert tool_result["status"] == "exited"
    assert tool_result["exit_code"] == 0
    assert _stdout(tool_result) == "from-provider\n"
    assert (tmp_path / "round-trip.txt").read_text() == "from-provider"

    assert history_payload["messages"][:4] == second_payload["messages"]
    assert history_payload["messages"][4] == {
        "role": "assistant",
        "content": final_text,
    }
    assert history_payload["messages"][5] == {
        "role": "user",
        "content": "Confirm the previous result is in History.",
    }


def _stream_response(*chunks: dict[str, object]) -> httpx.Response:
    text = (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=text,
    )


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _stdout(result: dict[str, object]) -> str:
    chunks = result["chunks"]
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
