import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ModelProvider,
    ScriptedModelProvider,
    ToolCall,
    ToolCallReady,
    ToolResultMessage,
    UserMessage,
)

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_public_runtime_combines_workspace_session_and_host_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text(
        "M5_COLLISION=workspace-collision\nM5_WORKSPACE=disk-initial\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("M5_HOST", "host-first")
    monkeypatch.setenv("M5_COLLISION", "host-collision")
    monkeypatch.setenv("CLI_AGENT_API_KEY", "provider-secret")

    export = ToolCall(
        call_id="a_export",
        name="exec",
        arguments={
            "command": ("export M5_COLLISION=session-collision M5_SESSION=present")
        },
    )
    inspect_a_first = _inspection_call("a_first", "a-first.json")
    inspect_a_second = _inspection_call("a_second", "a-second.json")
    inspect_b = _inspection_call("b_first", "b-first.json")
    inspect_fresh = _inspection_call("a_fresh", "a-fresh.json")
    inspect_later_runtime = _inspection_call("later_runtime", "later-runtime.json")
    provider_a = _scripted_provider(
        (export, inspect_a_first),
        (inspect_a_second,),
    )
    provider_b = _scripted_provider((inspect_b,))
    fresh_provider = _scripted_provider((inspect_fresh,))
    later_provider = _scripted_provider((inspect_later_runtime,))

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider_a,
            context_policy=_context_policy,
        )
        await _collect_turn(runtime, "session-a", "export and inspect")
        assert _load_json(tmp_path / "a-first.json") == {
            "CLI_AGENT_API_KEY": "provider-secret",
            "M5_COLLISION": "session-collision",
            "M5_HOST": "host-first",
            "M5_SESSION": "present",
            "M5_WORKSPACE": "disk-initial",
        }

        environment.write_text(
            "M5_COLLISION=workspace-collision\nM5_WORKSPACE=disk-later\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("M5_HOST", "host-later")
        await _collect_turn(runtime, "session-a", "inspect again")
        await _collect_turn(
            runtime,
            "session-b",
            "inspect independently",
            provider=provider_b,
        )

        assert _load_json(tmp_path / "a-second.json") == {
            "CLI_AGENT_API_KEY": "provider-secret",
            "M5_COLLISION": "session-collision",
            "M5_HOST": "host-later",
            "M5_SESSION": "present",
            "M5_WORKSPACE": "disk-initial",
        }
        expected_workspace_session = {
            "CLI_AGENT_API_KEY": "provider-secret",
            "M5_COLLISION": "workspace-collision",
            "M5_HOST": "host-later",
            "M5_WORKSPACE": "disk-initial",
        }
        assert _load_json(tmp_path / "b-first.json") == expected_workspace_session

        await runtime.close_session("session-a")
        await _collect_turn(
            runtime,
            "session-fresh",
            "inspect fresh",
            provider=fresh_provider,
        )
        assert _load_json(tmp_path / "a-fresh.json") == expected_workspace_session
        assert os.environ["M5_COLLISION"] == "host-collision"
        assert "M5_SESSION" not in os.environ

        _assert_result_statuses(
            provider_a, request_index=1, expected=("exited", "exited")
        )
        _assert_result_statuses(provider_a, request_index=3, expected=("exited",))
        _assert_result_statuses(provider_b, request_index=1, expected=("exited",))
        _assert_result_statuses(fresh_provider, request_index=1, expected=("exited",))
        await runtime.close()

        later_runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=later_provider,
            context_policy=_context_policy,
        )
        await _collect_turn(later_runtime, "session-later", "inspect later Runtime")
        assert _load_json(tmp_path / "later-runtime.json") == {
            "CLI_AGENT_API_KEY": "provider-secret",
            "M5_COLLISION": "workspace-collision",
            "M5_HOST": "host-later",
            "M5_WORKSPACE": "disk-later",
        }
        await later_runtime.close()

        for provider in (
            provider_a,
            provider_b,
            fresh_provider,
            later_provider,
        ):
            for request in provider.requests:
                assert tuple(tool.name for tool in request.tools) == (
                    "exec",
                    "output",
                    "kill",
                )
            provider.assert_exhausted()

    asyncio.run(scenario())


def _inspection_call(call_id: str, output_path: str) -> ToolCall:
    names = (
        "CLI_AGENT_API_KEY",
        "M5_COLLISION",
        "M5_HOST",
        "M5_SESSION",
        "M5_WORKSPACE",
    )
    source = (
        "import json, os; from pathlib import Path; "
        f"names = {names!r}; "
        f"Path({output_path!r}).write_text("
        "json.dumps({name: os.environ[name] for name in names if name in os.environ}, sort_keys=True)"
        ")"
    )
    return ToolCall(
        call_id=call_id,
        name="exec",
        arguments={"command": _python_command(source)},
    )


def _scripted_provider(
    *turn_calls: tuple[ToolCall, ...],
) -> ScriptedModelProvider:
    streams: list[tuple[ModelEvent, ...]] = []
    for index, calls in enumerate(turn_calls):
        tool_message = AssistantMessage(content=calls)
        streams.append(
            (
                *(ToolCallReady(call=call) for call in calls),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            )
        )
        streams.append(
            (
                ModelCompletion(
                    message=AssistantMessage.text(f"turn {index} complete"),
                    finish_reason="stop",
                ),
            )
        )
    return ScriptedModelProvider(script=streams)


async def _collect_turn(
    runtime: AgentRuntime,
    session_id: str,
    text: str,
    *,
    provider: ModelProvider | None = None,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(
                session_id,
                UserMessage.text(text),
                provider=provider,
            )
        ]
    )


def _assert_result_statuses(
    provider: ScriptedModelProvider,
    *,
    request_index: int,
    expected: tuple[str, ...],
) -> None:
    result_message = provider.requests[request_index].messages[-1]
    assert isinstance(result_message, ToolResultMessage)
    statuses = []
    for result in result_message.content:
        assert isinstance(result.output, dict)
        statuses.append(result.output["status"])
    assert tuple(statuses) == expected


def _load_json(path: Path) -> dict[str, str]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return {str(key): str(value) for key, value in loaded.items()}


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
