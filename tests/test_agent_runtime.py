import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

import cli_agent.runtime.runtime as runtime_module
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ExecutablePolicy,
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
        assert _TrackingEnvironmentKernel.instances == []

        await runtime.close()
        await runtime.close()

        assert runtime.closed
        assert _TrackingEnvironmentKernel.instances == []
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
        assert _TrackingEnvironmentKernel.instances == []
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
            execution_policy=ExecutablePolicy(
                denied_executables=frozenset({"echo"}),
            ),
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


def test_passes_default_and_configured_limits_to_kernel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    async def scenario() -> None:
        default_provider = ScriptedModelProvider(
            script=((_completion(AssistantMessage.text("default")),),)
        )
        configured_provider = ScriptedModelProvider(
            script=((_completion(AssistantMessage.text("configured")),),)
        )
        default_runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=default_provider,
        )
        configured_runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=configured_provider,
            pending_execution_capacity=7,
            parallel_execution_capacity=3,
            parallel_tool_execution_capacity=2,
            parallel_shell_commands=frozenset({"cat", "rg"}),
            parallel_tools=frozenset({"search", "fetch"}),
        )
        await _collect_turn(
            default_runtime,
            "default",
            UserMessage.text("default"),
        )
        await _collect_turn(
            configured_runtime,
            "configured",
            UserMessage.text("configured"),
        )

        assert [
            kernel.queue_limit
            for kernel in _TrackingEnvironmentKernel.instances
        ] == [32, 7]
        assert [
            kernel.parallel_limit
            for kernel in _TrackingEnvironmentKernel.instances
        ] == [4, 3]
        assert [
            kernel.parallel_commands
            for kernel in _TrackingEnvironmentKernel.instances
        ] == [frozenset(), frozenset({"cat", "rg"})]
        assert [
            kernel.tool_parallel_limit
            for kernel in _TrackingEnvironmentKernel.instances
        ] == [4, 2]
        assert [
            kernel.parallel_tools
            for kernel in _TrackingEnvironmentKernel.instances
        ] == [frozenset(), frozenset({"search", "fetch"})]
        assert [
            kernel.approval_session_id
            for kernel in _TrackingEnvironmentKernel.instances
        ] == ["default", "configured"]

        await default_runtime.close()
        await configured_runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "capacity",
    (True, False, 0, -1, 1.5, "2", None),
)
def test_rejects_invalid_pending_capacity_before_opening_environment(
    tmp_path: Path,
    monkeypatch,
    capacity: object,
) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    with pytest.raises(
        ValueError,
        match="pending_execution_capacity must be an integer >= 1",
    ):
        AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            pending_execution_capacity=capacity,  # type: ignore[arg-type]
        )

    assert _TrackingEnvironmentKernel.instances == []


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    (
        (
            "parallel_tool_execution_capacity",
            0,
            "parallel_execution_capacity must be an integer >= 1",
        ),
        (
            "parallel_tools",
            frozenset({"class"}),
            "parallel Tool names must be non-keyword Python identifiers",
        ),
    ),
)
def test_rejects_invalid_tool_parallel_configuration(
    tmp_path: Path,
    argument: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            **{argument: value},
        )


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    (
        (
            "pending_approval_capacity",
            0,
            "pending approval capacity must be an integer >= 1",
        ),
        (
            "approval_timeout_seconds",
            0,
            "approval timeout must be a number > 0",
        ),
    ),
)
def test_rejects_invalid_approval_limits_before_opening_environment(
    tmp_path: Path,
    monkeypatch,
    argument: str,
    value: object,
    message: str,
) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    with pytest.raises(ValueError, match=message):
        AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            **{argument: value},
        )

    assert _TrackingEnvironmentKernel.instances == []


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

        assert _TrackingEnvironmentKernel.instances == []
        assert (tmp_path / ".workspace").is_dir()
        assert (tmp_path / ".workspace" / "env").is_file()

    asyncio.run(scenario())


def test_closes_new_kernel_when_agent_loop_construction_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    class FailingAgentLoop:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise OpenFailure

    monkeypatch.setattr(runtime_module, "AgentLoop", FailingAgentLoop)

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )

        with pytest.raises(OpenFailure):
            await _collect_turn(
                runtime,
                "session-a",
                UserMessage.text("fail during Session construction"),
            )

        assert runtime._sessions == {}
        assert len(_TrackingEnvironmentKernel.instances) == 1
        assert _TrackingEnvironmentKernel.instances[0].close_count == 1
        await runtime.close()

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
        first_session = runtime._sessions["session-a"]
        first_kernel = first_session.kernel
        await runtime.close_session("session-a")
        await runtime.close_session("session-a")
        await runtime.close_session("unknown")
        await _collect_turn(runtime, "session-a", second_user)
        second_session = runtime._sessions["session-a"]

        first_system = provider.requests[0].messages[0]
        second_system = provider.requests[1].messages[0]
        assert isinstance(first_system, SystemMessage)
        assert isinstance(second_system, SystemMessage)
        assert provider.requests[0].messages == (first_system, first_user)
        assert provider.requests[1].messages == (second_system, second_user)
        assert second_system is not first_system
        assert first_kernel._closed is True
        assert second_session is not first_session
        assert second_session.loop is not first_session.loop
        assert second_session.kernel is not first_kernel
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
        assert ".workspace/tools" in text
        assert ".workspace/skills" in text
        assert ".workspace/library" in text
        assert "`exec`, `output`, and `kill`" in text
        assert "not an operating-system security boundary" in text
        assert text.endswith("Host instruction\nPrefer focused, reversible changes.")
        provider.assert_exhausted()
        await runtime.close()

    asyncio.run(scenario())


def test_runtime_closes_every_session_kernel(
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

        assert len(_TrackingEnvironmentKernel.instances) == 2
        assert [
            kernel.close_count for kernel in _TrackingEnvironmentKernel.instances
        ] == [1, 1]
        assert [
            kernel.events for kernel in _TrackingEnvironmentKernel.instances
        ] == [["kernel.close"], ["kernel.close"]]
        await runtime.close_session("session-a")
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await _collect_turn(runtime, "session-a", UserMessage.text("closed"))

    asyncio.run(scenario())


class OpenFailure(Exception):
    pass


class _TrackingEnvironmentKernel:
    instances: list["_TrackingEnvironmentKernel"] = []

    def __init__(
        self,
        workspace: str | Path,
        *,
        base_env: Mapping[str, str],
        policy: object,
        capability_view: object,
        tool_catalog: object,
        tool_environment: object,
        approval_gate: object,
        approval_session_id: str,
        queue_limit: int,
        parallel_limit: int,
        tool_parallel_limit: int,
        parallel_commands: frozenset[str],
        parallel_tools: frozenset[str],
    ) -> None:
        self.workspace = Path(workspace)
        self.base_env = base_env
        self.policy = policy
        self.capability_view = capability_view
        self.tool_catalog = tool_catalog
        self.tool_environment = tool_environment
        self.approval_gate = approval_gate
        self.approval_session_id = approval_session_id
        self.queue_limit = queue_limit
        self.parallel_limit = parallel_limit
        self.tool_parallel_limit = tool_parallel_limit
        self.parallel_commands = parallel_commands
        self.parallel_tools = parallel_tools
        self.close_count = 0
        self.events: list[str] = []
        self.instances.append(self)

    async def dispatch(self, call: ToolCall) -> ToolResult:
        raise AssertionError(f"unexpected Tool Call: {call}")

    async def dispatch_batch(
        self,
        calls: tuple[ToolCall, ...],
    ) -> tuple[ToolResult, ...]:
        raise AssertionError(f"unexpected Tool Calls: {calls}")

    async def close(self) -> None:
        self.close_count += 1
        self.events.append("kernel.close")


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
