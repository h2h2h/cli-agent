import asyncio
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction
from policy_fakes import _DenyExecutablePolicy

import cli_agent.runtime.runtime as runtime_module
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    PolicyAction,
    PolicyEvaluation,
    RuntimeClosedError,
    ScriptedModelProvider,
    SystemMessage,
    ToolCall,
    ToolCallReady,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from cli_agent.runtime._backend import _BackendWorkspace, _BoundCapabilityView
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._resources import _RuntimeResources

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
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
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
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
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )
        async with runtime:
            assert not runtime.closed

        assert runtime.closed
        assert _TrackingEnvironmentKernel.instances == []
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            async with runtime:
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
        async with await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            execution_policy=_DenyExecutablePolicy(
                frozenset({"echo"}),
                reason="direct invocation of 'echo' is denied by policy",
            ),
            context_policy=_context_policy,
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


def test_open_requires_user_interaction_and_creates_no_default(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )


def test_passes_parallel_command_authorization_to_kernel(
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
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=default_provider,
            context_policy=_context_policy,
        )
        configured_runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=configured_provider,
            parallel_commands=frozenset({"cat", "rg"}),
            context_policy=_context_policy,
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
            kernel.parallel_commands for kernel in _TrackingEnvironmentKernel.instances
        ] == [frozenset(), frozenset({"cat", "rg"})]
        assert [
            kernel.session_id for kernel in _TrackingEnvironmentKernel.instances
        ] == ["default", "configured"]

        await default_runtime.close()
        await configured_runtime.close()

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
                user_interaction=_user_interaction,
                context_policy=_context_policy,
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
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
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
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=default_provider,
            context_policy=_context_policy,
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


def test_serializes_concurrent_turns_on_the_same_session(tmp_path: Path) -> None:
    class PausingProvider:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.requests: list[ModelRequest] = []

        async def generate(
            self,
            request: ModelRequest,
        ) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            if len(self.requests) == 1:
                self.entered.set()
                await self.release.wait()
            yield ModelCompletion(
                message=AssistantMessage.text("Done"),
                finish_reason="stop",
            )

    provider = PausingProvider()

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        first_turn = asyncio.create_task(
            _collect_turn(runtime, "session-a", UserMessage.text("First"))
        )
        await provider.entered.wait()
        second_turn = asyncio.create_task(
            _collect_turn(runtime, "session-a", UserMessage.text("Second"))
        )
        await asyncio.sleep(0)
        assert len(provider.requests) == 1

        provider.release.set()
        first_events, second_events = await asyncio.gather(first_turn, second_turn)

        assert first_events == (_completion(AssistantMessage.text("Done")),)
        assert second_events == (_completion(AssistantMessage.text("Done")),)
        assert len(provider.requests) == 2
        system_message = provider.requests[0].messages[0]
        assert provider.requests[0].messages == (
            system_message,
            UserMessage.text("First"),
        )
        assert provider.requests[1].messages == (
            system_message,
            UserMessage.text("First"),
            AssistantMessage.text("Done"),
            UserMessage.text("Second"),
        )
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
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
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
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            system_instruction="Prefer focused, reversible changes.",
            context_policy=_context_policy,
        )

        await _collect_turn(runtime, "session-a", UserMessage.text("Work"))

        system_message = provider.requests[0].messages[0]
        assert isinstance(system_message, SystemMessage)
        text = "".join(block.text for block in system_message.content)
        assert "You are cli-agent" in text
        assert f"The bound Workspace is {tmp_path.resolve()};" in text
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
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
        )
        await _collect_turn(runtime, "session-a", UserMessage.text("A"))
        await _collect_turn(runtime, "session-b", UserMessage.text("B"))

        await runtime.close()
        await runtime.close()

        assert len(_TrackingEnvironmentKernel.instances) == 2
        assert [
            kernel.close_count for kernel in _TrackingEnvironmentKernel.instances
        ] == [1, 1]
        assert [kernel.events for kernel in _TrackingEnvironmentKernel.instances] == [
            ["kernel.close"],
            ["kernel.close"],
        ]
        await runtime.close_session("session-a")
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            await _collect_turn(runtime, "session-a", UserMessage.text("closed"))

    asyncio.run(scenario())


def test_runtime_holds_single_resource_aggregate(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )

        resources = runtime._resources
        assert isinstance(resources, _RuntimeResources)
        assert resources.workspace == tmp_path.resolve()
        assert isinstance(resources.backend, _BackendWorkspace)
        assert isinstance(resources.capability_view, _BoundCapabilityView)
        assert isinstance(resources.tool_catalog, _ToolCatalog)
        assert isinstance(resources.skill_catalog, _SkillCatalog)
        assert not hasattr(runtime, "_workspace")
        assert not hasattr(runtime, "_backend")
        assert not hasattr(runtime, "_capability_view")
        assert not hasattr(runtime, "_tool_catalog")
        assert not hasattr(runtime, "_skill_catalog")
        assert not hasattr(runtime, "_mcp_catalog")
        assert not hasattr(runtime, "_base_env")
        await runtime.close()

    asyncio.run(scenario())


def test_sessions_borrow_the_same_workspace_resources(
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
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        await _collect_turn(runtime, "session-a", UserMessage.text("A"))
        await _collect_turn(runtime, "session-b", UserMessage.text("B"))

        assert len(_TrackingEnvironmentKernel.instances) == 2
        first, second = _TrackingEnvironmentKernel.instances
        resources = runtime._resources
        assert first.backend is resources.backend
        assert second.backend is resources.backend
        assert first.backend.capabilities is resources.capability_view
        assert second.backend.capabilities is resources.capability_view
        assert first.tool_catalog is resources.tool_catalog
        assert second.tool_catalog is resources.tool_catalog
        await runtime.close()

    asyncio.run(scenario())


def test_each_session_gets_an_independent_environment_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text("VALUE=shared\n", encoding="utf-8")
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
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        await _collect_turn(runtime, "session-a", UserMessage.text("A"))
        await _collect_turn(runtime, "session-b", UserMessage.text("B"))

        first, second = _TrackingEnvironmentKernel.instances
        assert first.base_env == {"VALUE": "shared"}
        assert second.base_env == {"VALUE": "shared"}
        assert first.base_env is not second.base_env
        assert first.base_env is not runtime._resources.base_env
        await runtime.close()

    asyncio.run(scenario())


def test_runtime_close_only_closes_session_owned_state(
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
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        resources = runtime._resources
        await _collect_turn(runtime, "session-a", UserMessage.text("A"))
        await _collect_turn(runtime, "session-b", UserMessage.text("B"))
        await runtime.close_session("session-a")

        assert [
            kernel.close_count for kernel in _TrackingEnvironmentKernel.instances
        ] == [1, 0]

        await runtime.close()

        assert [
            kernel.close_count for kernel in _TrackingEnvironmentKernel.instances
        ] == [1, 1]
        assert resources.backend._closed
        assert hasattr(resources, "close")
        assert resources.workspace == tmp_path.resolve()
        assert resources.base_env == {}
        assert resources.capability_view is runtime._resources.capability_view
        assert resources.tool_catalog is runtime._resources.tool_catalog
        assert resources.skill_catalog is runtime._resources.skill_catalog

    asyncio.run(scenario())


def test_host_owned_dependencies_stay_outside_the_aggregate(
    tmp_path: Path,
) -> None:
    class _TrackingProvider(ScriptedModelProvider):
        def __init__(self) -> None:
            super().__init__(script=((_completion(AssistantMessage.text("Done")),),))
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _TrackingPolicy:
        def __init__(self) -> None:
            self.closed = False

        async def evaluate(self, command: object) -> PolicyEvaluation:
            del command
            return PolicyEvaluation(
                action=PolicyAction.ALLOW,
                rule_id="test.allow",
            )

        def close(self) -> None:
            self.closed = True

    class _TrackingInteraction:
        def __init__(self) -> None:
            self.closed = False

        async def ask(self, request: object) -> object:
            raise AssertionError("interaction must not be invoked")

        def close(self) -> None:
            self.closed = True

    provider = _TrackingProvider()
    policy = _TrackingPolicy()
    interaction = _TrackingInteraction()
    received: list[object] = []

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=interaction,
            workspace=tmp_path,
            provider=provider,
            execution_policy=policy,
            on_diagnostic=received.append,
            context_policy=_context_policy,
        )
        await _collect_turn(runtime, "session-a", UserMessage.text("Work"))
        await runtime.close()

        field_names = set(_RuntimeResources.__dataclass_fields__)
        assert field_names.isdisjoint(
            {"provider", "policy", "user_interaction", "on_diagnostic"}
        )
        assert not provider.closed
        assert not policy.closed
        assert not interaction.closed

    asyncio.run(scenario())


class OpenFailure(Exception):
    pass


class _TrackingEnvironmentKernel:
    instances: list["_TrackingEnvironmentKernel"] = []

    def __init__(
        self,
        workspace: str | Path,
        *,
        backend: object,
        base_env: Mapping[str, str],
        policy: object,
        library_catalog: object,
        tool_catalog: object,
        user_interaction: object,
        session_id: str,
        parallel_commands: frozenset[str],
        on_diagnostic: object | None,
    ) -> None:
        self.workspace = Path(workspace)
        self.backend = backend
        self.base_env = dict(base_env or {})
        self.policy = policy
        self.library_catalog = library_catalog
        self.tool_catalog = tool_catalog
        self.user_interaction = user_interaction
        self.session_id = session_id
        self.parallel_commands = parallel_commands
        self.on_diagnostic = on_diagnostic
        self.close_count = 0
        self.events: list[str] = []
        self.instances.append(self)

    async def reconcile_library(self) -> None:
        return

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
