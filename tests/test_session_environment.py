import asyncio
from pathlib import Path

from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    UserMessage,
)
from cli_agent.runtime._environment import EnvironmentKernel

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_sessions_own_isolated_copies_of_runtime_open_environment(
    tmp_path: Path,
) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text("VALUE=workspace\n", encoding="utf-8")
    provider = ScriptedModelProvider(
        script=tuple(
            (
                ModelCompletion(
                    message=AssistantMessage.text(text), finish_reason="stop"
                ),
            )
            for text in ("first a", "first b", "second a", "fresh a")
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )

        await _collect_turn(runtime, "session-a", "first a")
        await _collect_turn(runtime, "session-b", "first b")
        session_a = _session_kernel(runtime, "session-a")
        session_b = _session_kernel(runtime, "session-b")

        assert session_a._env == {"VALUE": "workspace"}
        assert session_b._env == {"VALUE": "workspace"}
        assert session_a._env is not session_b._env

        session_a._env["VALUE"] = "session-a"
        session_a._env["SESSION_ONLY"] = "present"
        await _collect_turn(runtime, "session-a", "second a")

        assert _session_kernel(runtime, "session-a") is session_a
        assert session_a._env == {
            "SESSION_ONLY": "present",
            "VALUE": "session-a",
        }
        assert session_b._env == {"VALUE": "workspace"}

        environment.write_text(
            "VALUE=later edit\nLATER=not loaded\n",
            encoding="utf-8",
        )
        await runtime.close_session("session-a")
        assert session_a._env == {}

        await _collect_turn(runtime, "session-fresh", "fresh a")
        fresh_session_a = _session_kernel(runtime, "session-fresh")
        assert fresh_session_a is not session_a
        assert fresh_session_a._env == {"VALUE": "workspace"}
        assert session_b._env == {"VALUE": "workspace"}

        await runtime.close()
        assert fresh_session_a._env == {}
        assert session_b._env == {}
        provider.assert_exhausted()

    asyncio.run(scenario())


async def _collect_turn(
    runtime: AgentRuntime,
    session_id: str,
    text: str,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(
                session_id,
                UserMessage.text(text),
            )
        ]
    )


def _session_kernel(
    runtime: AgentRuntime,
    session_id: str,
) -> EnvironmentKernel:
    return runtime._sessions[session_id].kernel
