import ast
import json
import shlex
import socket
import sys
from io import StringIO
from pathlib import Path

import httpx

import cli_agent.cli as cli_module
from cli_agent.cli import main
from cli_agent.config import (
    API_KEY_ENV,
    BASE_URL_ENV,
    MODEL_ENV,
    build_provider,
)
from cli_agent.runtime import AgentRuntime, ModelUsage, ToolCall


def test_proves_cli_agent_offline_through_real_provider_adapter(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)

    command = _python_command(
        "from pathlib import Path; "
        "Path('cli-proof.txt').write_text('from-cli'); "
        "print(Path('cli-proof.txt').read_text())"
    )
    call = ToolCall(
        call_id="call_exec",
        name="exec",
        arguments={"command": command},
    )
    encoded_arguments = json.dumps(call.arguments)
    split_at = len(encoded_arguments) // 2
    final_usage = ModelUsage(
        input_tokens=35,
        output_tokens=7,
        total_tokens=42,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return _stream_response(
                {
                    "choices": [
                        {
                            "delta": {"content": "Running command. "},
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

        if len(requests) == 2:
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
                            "delta": {"content": "from-cli"},
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

        raise AssertionError("unexpected model request")

    transport = httpx.MockTransport(respond)

    def build_offline_provider(config):
        return build_provider(config, transport=transport)

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

    monkeypatch.setattr(cli_module, "build_provider", build_offline_provider)
    monkeypatch.setattr(AgentRuntime, "close_session", tracking_close_session)
    monkeypatch.setattr(AgentRuntime, "close", tracking_close)
    monkeypatch.setenv(MODEL_ENV, "test-model")
    monkeypatch.setenv(BASE_URL_ENV, "https://models.invalid/v1")
    monkeypatch.setenv(API_KEY_ENV, "offline-placeholder-key")

    exit_code = main(
        [
            "Create the CLI proof file.",
            "--workspace",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "Running command. Command output: from-cli"
    assert captured.err == (
        f"[tool] exec: {command}\n"
        "[completion] reason=stop usage=input:35,output:7,total:42\n"
    )
    assert "offline-placeholder-key" not in captured.out
    assert "offline-placeholder-key" not in captured.err
    assert lifecycle == ["session", "runtime"]
    assert (tmp_path / "cli-proof.txt").read_text() == "from-cli"

    assert len(requests) == 2
    assert all(
        request.headers["authorization"] == "Bearer offline-placeholder-key"
        for request in requests
    )
    first_payload, second_payload = (
        json.loads(request.content) for request in requests
    )

    assert first_payload["messages"][0]["role"] == "system"
    assert str(tmp_path.resolve()) in first_payload["messages"][0]["content"]
    assert first_payload["messages"][1] == {
        "role": "user",
        "content": "Create the CLI proof file.",
    }
    assert [tool["function"]["name"] for tool in first_payload["tools"]] == [
        "exec",
        "output",
        "kill",
    ]
    assert first_payload["model"] == "test-model"
    assert first_payload["stream"] is True
    assert first_payload["stream_options"] == {"include_usage": True}

    assert second_payload["messages"][:2] == first_payload["messages"]
    assistant_payload = second_payload["messages"][2]
    assert assistant_payload["role"] == "assistant"
    assert assistant_payload["content"] == "Running command. "
    assert assistant_payload["tool_calls"] == [
        {
            "id": call.call_id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": encoded_arguments,
            },
        }
    ]

    tool_result_payload = second_payload["messages"][3]
    assert tool_result_payload["role"] == "tool"
    assert tool_result_payload["tool_call_id"] == call.call_id
    tool_result = json.loads(tool_result_payload["content"])
    assert tool_result["ok"] is True
    assert tool_result["status"] == "exited"
    assert tool_result["exit_code"] == 0
    assert _stdout(tool_result) == "from-cli\n"


def test_proves_interactive_history_offline_through_real_provider_adapter(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)

    requests: list[httpx.Request] = []
    responses = ("First response", "Second response")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) > len(responses):
            raise AssertionError("unexpected model request")
        return _stream_response(
            {
                "choices": [
                    {
                        "delta": {"content": responses[len(requests) - 1]},
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

    transport = httpx.MockTransport(respond)

    def build_offline_provider(config):
        return build_provider(config, transport=transport)

    monkeypatch.setattr(cli_module, "build_provider", build_offline_provider)
    monkeypatch.setattr(
        cli_module.sys,
        "stdin",
        StringIO("\nFirst turn\nSecond turn\n:q\n"),
    )
    monkeypatch.setenv(MODEL_ENV, "test-model")
    monkeypatch.setenv(BASE_URL_ENV, "https://models.invalid/v1")
    monkeypatch.setenv(API_KEY_ENV, "offline-placeholder-key")

    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "First response\nSecond response\n"
    assert captured.err == ("[completion] reason=stop\n[completion] reason=stop\n")
    assert len(requests) == 2

    first_payload, second_payload = (
        json.loads(request.content) for request in requests
    )
    system_message = first_payload["messages"][0]
    assert first_payload["messages"] == [
        system_message,
        {"role": "user", "content": "First turn"},
    ]
    assert second_payload["messages"] == [
        system_message,
        {"role": "user", "content": "First turn"},
        {"role": "assistant", "content": "First response"},
        {"role": "user", "content": "Second turn"},
    ]


def test_cli_uses_only_the_public_runtime_and_owns_no_execution_code() -> None:
    cli_package = Path(cli_module.__file__).parent
    module_paths = (
        cli_package / "cli.py",
        cli_package / "config.py",
        cli_package / "presentation.py",
        cli_package / "runner.py",
    )
    forbidden_calls = {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "exec",
        "popen",
        "system",
    }

    for module_path in module_paths:
        tree = ast.parse(module_path.read_text())

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith(
                    "cli_agent.runtime."
                )
                assert node.module not in {"asyncio.subprocess", "subprocess"}
            if isinstance(node, ast.Import):
                assert all(
                    not alias.name.startswith(
                        ("cli_agent.runtime.", "asyncio.subprocess", "subprocess")
                    )
                    for alias in node.names
                )
            if isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    assert function.id not in forbidden_calls
                if isinstance(function, ast.Attribute):
                    assert function.attr not in forbidden_calls


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


def _deny_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("network access is forbidden in this scenario")
