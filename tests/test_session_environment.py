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
    UserMessage,
)
from cli_agent.runtime._environment import EnvironmentKernel

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_bindings_own_isolated_copies_of_runtime_open_environment(
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
            for text in ("first a", "second a", "first b", "fresh a")
        )
    )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=provider,
            interaction=_user_interaction,
            context_policy=_context_policy,
        )

        await runtime.new_session()
        await _collect_turn(runtime, "first a")
        session_a = runtime._binding.kernel
        assert session_a._env == {"VALUE": "workspace"}

        session_a._env["VALUE"] = "session-a"
        session_a._env["SESSION_ONLY"] = "present"
        await _collect_turn(runtime, "second a")
        assert runtime._binding.kernel is session_a
        assert session_a._env == {
            "SESSION_ONLY": "present",
            "VALUE": "session-a",
        }

        await runtime.detach_session()
        assert session_a._env == {}

        await runtime.new_session()
        await _collect_turn(runtime, "first b")
        session_b = runtime._binding.kernel
        assert session_b._env == {"VALUE": "workspace"}
        assert session_b._env is not session_a._env
        session_b._env["SESSION_ONLY"] = "present-b"

        await runtime.new_session()
        await _collect_turn(runtime, "fresh a")
        fresh = runtime._binding.kernel
        assert fresh is not session_b
        assert fresh._env == {"VALUE": "workspace"}

        await runtime.close()
        assert fresh._env == {}
        provider.assert_exhausted()

    asyncio.run(scenario())


async def _collect_turn(
    runtime: AgentRuntime,
    text: str,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(UserMessage.text(text))
        ]
    )


def _session_kernel(runtime: AgentRuntime) -> EnvironmentKernel:
    binding = runtime._binding
    assert binding is not None
    return binding.kernel
