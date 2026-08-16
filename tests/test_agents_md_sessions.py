"""Workspace instruction injection across Session and Runtime lifecycles."""

from __future__ import annotations

import asyncio
from pathlib import Path

from interaction_fakes import _ScriptedInteraction

from cli_agent.presets import open_default_runtime
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    SystemMessage,
    UserMessage,
)

_user_interaction = _ScriptedInteraction("deny")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)

_RULES_V1 = "# Project rules\n\nrun `uv run pytest` before review.\n"
_RULES_V2 = "# Project rules\n\nuse `make test` instead.\n"


def _plain_step(text: str) -> tuple[ModelEvent, ...]:
    return (ModelCompletion(message=AssistantMessage.text(text), finish_reason="stop"),)


async def _open_runtime(
    tmp_path: Path,
    provider: ScriptedModelProvider,
) -> AgentRuntime:
    return await open_default_runtime(
        workspace=tmp_path,
        provider=provider,
        interaction=_user_interaction,
        context_policy=_context_policy,
    )


async def _run_turn(
    runtime: AgentRuntime,
    text: str = "go",
) -> tuple[ModelEvent, ...]:
    await runtime.new_session()
    return tuple(
        [event async for event in runtime.run_turn(UserMessage.text(text))]
    )


def _system_text(request: object) -> str:
    message = request.messages[0]
    assert isinstance(message, SystemMessage)
    return "\n".join(block.text for block in message.content)


def test_first_model_request_contains_workspace_instructions(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text(_RULES_V1, encoding="utf-8")
    provider = ScriptedModelProvider(script=(_plain_step("done"),))

    async def scenario() -> None:
        async with await _open_runtime(tmp_path, provider) as runtime:
            await _run_turn(runtime)

        body = _system_text(provider.requests[0])
        assert "**Workspace instructions**" in body
        assert f"Source: {tmp_path.resolve() / 'AGENTS.md'}" in body
        assert (
            "they conflict with the Runtime protocol, Host instructions, "
            "or an explicit current user request" in body
        )
        assert _RULES_V1 in body
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_absent_agents_md_keeps_system_message_unchanged(tmp_path: Path) -> None:
    provider = ScriptedModelProvider(script=(_plain_step("done"),))

    async def scenario() -> None:
        async with await _open_runtime(tmp_path, provider) as runtime:
            await _run_turn(runtime)

        assert "**Workspace instructions**" not in _system_text(provider.requests[0])
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_sessions_share_snapshot_and_file_change_does_not_hot_reload(
    tmp_path: Path,
) -> None:
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(_RULES_V1, encoding="utf-8")
    provider = ScriptedModelProvider(
        script=(
            _plain_step("a done"),
            _plain_step("b done"),
            _plain_step("c done"),
            _plain_step("d done"),
        )
    )

    async def scenario() -> None:
        runtime = await _open_runtime(tmp_path, provider)
        try:
            await _run_turn(runtime)
            await _run_turn(runtime)

            agents_md.write_text(_RULES_V2, encoding="utf-8")
            await _run_turn(runtime)

            bodies = tuple(_system_text(request) for request in provider.requests[:3])
            assert all(_RULES_V1 in body for body in bodies)
            assert all(_RULES_V2 not in body for body in bodies)
        finally:
            await runtime.close()

        async with await _open_runtime(tmp_path, provider) as reopened:
            await _run_turn(reopened)

        body = _system_text(provider.requests[3])
        assert _RULES_V2 in body
        assert _RULES_V1 not in body
        provider.assert_exhausted()

    asyncio.run(scenario())
