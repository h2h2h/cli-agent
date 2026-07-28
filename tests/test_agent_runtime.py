import asyncio
from pathlib import Path

import pytest

import cli_agent.runtime.runtime as runtime_module
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ModelProvider,
    RuntimeClosedError,
    ScriptedModelProvider,
    SystemMessage,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)


def test_opens_and_closes_runtime_explicitly(tmp_path: Path, monkeypatch) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )

        assert not runtime.closed
        assert len(_TrackingEnvironmentKernel.instances) == 1

        await runtime.close()
        await runtime.close()

        assert runtime.closed
        assert _TrackingEnvironmentKernel.instances[0].close_count == 1
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            async with runtime:
                pass

    asyncio.run(scenario())


def test_closes_runtime_context_manager(tmp_path: Path, monkeypatch) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    async def scenario() -> None:
        opener = AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )
        async with opener as runtime:
            assert not runtime.closed

        assert runtime.closed
        assert len(_TrackingEnvironmentKernel.instances) == 1
        assert _TrackingEnvironmentKernel.instances[0].close_count == 1
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            async with opener:
                pass

    asyncio.run(scenario())


def test_host_configures_runtime_lifetime_executable_deny_set(
    tmp_path: Path,
) -> None:
    call = ToolCall(
        call_id="deny_echo",
        name="exec",
        arguments={"command": "echo must-not-run"},
    )
    tool_message = AssistantMessage(content=(call,))
    final_message = AssistantMessage.text("Denied.")
    provider = ScriptedModelProvider(
        script=(
            (
                ToolCallReady(call=call),
                ModelCompletion(
                    message=tool_message,
                    finish_reason="tool_calls",
                ),
            ),
            (
                ModelCompletion(
                    message=final_message,
                    finish_reason="stop",
                ),
            ),
        )
    )

    async def scenario() -> None:
        async with AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            denied_executables=frozenset({"echo"}),
        ) as runtime:
            await _collect_turn(runtime, "session", UserMessage.text("Run echo"))

        result_message = provider.requests[1].messages[-1]
        assert isinstance(result_message, ToolResultMessage)
        result = result_message.content[0]
        assert result.error == {
            "ok": False,
            "code": "policy_denied",
            "message": "direct invocation of 'echo' is denied by policy",
        }
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_cleans_up_environment_when_open_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    class FailingAgentRuntime(AgentRuntime):
        def __init__(self, **kwargs: object) -> None:
            raise OpenFailure

    async def scenario() -> None:
        with pytest.raises(OpenFailure):
            await FailingAgentRuntime.open(
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
            )

        assert len(_TrackingEnvironmentKernel.instances) == 1
        assert _TrackingEnvironmentKernel.instances[0].close_count == 1

    asyncio.run(scenario())


def test_reuses_session_history_and_bound_provider(tmp_path: Path) -> None:
    first_user = UserMessage.text("First turn")
    second_user = UserMessage.text("Second turn")
    first_assistant = AssistantMessage.text("First response")
    second_assistant = AssistantMessage.text("Second response")
    default_provider = ScriptedModelProvider(script=())
    session_provider = ScriptedModelProvider(
        script=(
            (_completion(first_assistant),),
            (_completion(second_assistant),),
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=default_provider,
        )

        first_events = await _collect_turn(
            runtime,
            "session-a",
            first_user,
            provider=session_provider,
        )
        second_events = await _collect_turn(
            runtime,
            "session-a",
            second_user,
            provider=default_provider,
        )

        assert first_events == (_completion(first_assistant),)
        assert second_events == (_completion(second_assistant),)
        system_message = session_provider.requests[0].messages[0]
        assert isinstance(system_message, SystemMessage)
        assert session_provider.requests[0].messages == (
            system_message,
            first_user,
        )
        assert session_provider.requests[1].messages == (
            system_message,
            first_user,
            first_assistant,
            second_user,
        )
        assert session_provider.requests[1].messages[0] is system_message
        assert default_provider.requests == ()
        session_provider.assert_exhausted()
        default_provider.assert_exhausted()
        await runtime.close()

    asyncio.run(scenario())


def test_reusing_closed_session_id_creates_fresh_state(tmp_path: Path) -> None:
    first_user = UserMessage.text("Before close")
    second_user = UserMessage.text("After close")
    first_assistant = AssistantMessage.text("Old state")
    second_assistant = AssistantMessage.text("Fresh state")
    provider = ScriptedModelProvider(
        script=(
            (_completion(first_assistant),),
            (_completion(second_assistant),),
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
        )

        await _collect_turn(runtime, "session-a", first_user)
        await runtime.close_session("session-a")
        await runtime.close_session("session-a")
        await runtime.close_session("unknown")
        await _collect_turn(runtime, "session-a", second_user)

        first_system = provider.requests[0].messages[0]
        second_system = provider.requests[1].messages[0]
        assert isinstance(first_system, SystemMessage)
        assert isinstance(second_system, SystemMessage)
        assert provider.requests[0].messages == (first_system, first_user)
        assert provider.requests[1].messages == (second_system, second_user)
        assert second_system is not first_system
        provider.assert_exhausted()
        await runtime.close()

    asyncio.run(scenario())


def test_assembles_workspace_and_optional_host_instruction(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider(
        script=((_completion(AssistantMessage.text("Done")),),),
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            system_instruction="Prefer focused, reversible changes.",
        )

        await _collect_turn(runtime, "session-a", UserMessage.text("Work"))

        system_message = provider.requests[0].messages[0]
        assert isinstance(system_message, SystemMessage)
        text = "".join(block.text for block in system_message.content)
        assert "You are cli-agent" in text
        assert f"The bound Workspace is {tmp_path.resolve()}." in text
        assert "exec, output, and kill" in text
        assert "not an operating-system security boundary" in text
        assert text.endswith("Host instruction\nPrefer focused, reversible changes.")
        provider.assert_exhausted()
        await runtime.close()

    asyncio.run(scenario())


def test_runtime_closes_all_sessions_before_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )
    provider = ScriptedModelProvider(
        script=(
            (_completion(AssistantMessage.text("A")),),
            (_completion(AssistantMessage.text("B")),),
        )
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
        )
        await _collect_turn(runtime, "session-a", UserMessage.text("A"))
        await _collect_turn(runtime, "session-b", UserMessage.text("B"))

        await runtime.close()
        await runtime.close()

        kernel = _TrackingEnvironmentKernel.instances[0]
        assert [binding.close_count for binding in kernel.bindings] == [1, 1]
        assert kernel.close_count == 1
        assert kernel.events == [
            "binding.close",
            "binding.close",
            "kernel.close",
        ]
        await runtime.close_session("session-a")
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await _collect_turn(runtime, "session-a", UserMessage.text("closed"))

    asyncio.run(scenario())


class OpenFailure(Exception):
    pass


class _TrackingEnvironmentKernel:
    instances: list["_TrackingEnvironmentKernel"] = []

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.close_count = 0
        self.events: list[str] = []
        self.bindings: list[_TrackingEnvironmentBinding] = []
        self.instances.append(self)

    def create_binding(self) -> "_TrackingEnvironmentBinding":
        binding = _TrackingEnvironmentBinding(self.events)
        self.bindings.append(binding)
        return binding

    async def close(self) -> None:
        self.close_count += 1
        self.events.append("kernel.close")


class _TrackingEnvironmentBinding:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.close_count = 0

    async def dispatch(self, call: ToolCall) -> ToolResult:
        raise AssertionError(f"unexpected Tool Call: {call}")

    async def close(self) -> None:
        self.close_count += 1
        self._events.append("binding.close")


def _completion(message: AssistantMessage) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop")


async def _collect_turn(
    runtime: AgentRuntime,
    session_id: str,
    message: UserMessage,
    *,
    provider: ModelProvider | None = None,
) -> tuple[ModelEvent, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(
                session_id,
                message,
                provider=provider,
            )
        ]
    )
