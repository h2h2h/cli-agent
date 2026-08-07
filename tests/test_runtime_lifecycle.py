"""Issue 10: Backend Workspace lifecycle enforcement tests.

These tests pin the RFC-0012 issue 10 acceptance criteria: the Runtime close
follows the documented reverse-dependency order, partial-open failures roll
back every already-opened resource, flush/close failures are visible to the
Host without hiding persistence failure, the Library worker is cancelled on
close, running Executions are terminated, repeated close stays idempotent,
and Backend constraint failures never create a Local execution.
"""

import asyncio
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime._capability.library.catalog as library_module
import cli_agent.runtime._resources as resources_module
import cli_agent.runtime.runtime as runtime_module
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    RuntimeClosedError,
    ScriptedModelProvider,
    ToolCall,
    ToolCallReady,
    UserMessage,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


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
        del workspace, backend, base_env, policy, library_catalog, tool_catalog
        del user_interaction, session_id, parallel_commands, on_diagnostic
        self.close_count = 0
        self.events: list[str] = []
        self.instances.append(self)

    async def reconcile_library(self) -> None:
        return

    async def dispatch(self, call: ToolCall) -> object:
        raise AssertionError(f"unexpected Tool Call: {call}")

    async def dispatch_batch(
        self,
        calls: tuple[ToolCall, ...],
    ) -> tuple[object, ...]:
        raise AssertionError(f"unexpected Tool Calls: {calls}")

    async def close(self) -> None:
        self.close_count += 1
        self.events.append("kernel.close")


def _completion(message: AssistantMessage) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop")


def test_runtime_close_follows_rfc_close_order(tmp_path: Path, monkeypatch) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )
    provider = ScriptedModelProvider(
        script=((_completion(AssistantMessage.text("A")),),)
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
        )
        async for _ in runtime.run_turn("session-a", UserMessage.text("A")):
            pass
        order: list[str] = []
        library = runtime._resources.library_catalog
        backend = runtime._resources.backend

        async def record_kernel_close() -> None:
            order.append("kernel.close")

        async def record_library_close() -> None:
            order.append("library.close")

        async def record_flush() -> None:
            order.append("flush")

        async def record_backend_close() -> None:
            order.append("backend.close")

        monkeypatch.setattr(
            _TrackingEnvironmentKernel.instances[0],
            "close",
            record_kernel_close,
        )
        monkeypatch.setattr(library, "close", record_library_close)
        monkeypatch.setattr(backend, "flush", record_flush)
        monkeypatch.setattr(backend, "close", record_backend_close)

        await runtime.close()

        assert order == [
            "kernel.close",
            "library.close",
            "flush",
            "backend.close",
        ]
        assert runtime.closed
        assert _TrackingEnvironmentKernel.instances[0].close_count == 0

    asyncio.run(scenario())


def test_kernel_close_failure_does_not_leak_resources(
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
        script=((_completion(AssistantMessage.text("A")),),)
    )
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
            on_diagnostic=received.append,
        )
        async for _ in runtime.run_turn("session-a", UserMessage.text("A")):
            pass

        closed: list[str] = []
        library = runtime._resources.library_catalog
        backend = runtime._resources.backend

        async def failing_kernel_close() -> None:
            raise RuntimeError("kernel close exploded")

        async def record_library_close() -> None:
            closed.append("library.close")

        async def record_flush() -> None:
            closed.append("flush")

        async def record_backend_close() -> None:
            closed.append("backend.close")

        monkeypatch.setattr(
            _TrackingEnvironmentKernel.instances[0],
            "close",
            failing_kernel_close,
        )
        monkeypatch.setattr(library, "close", record_library_close)
        monkeypatch.setattr(backend, "flush", record_flush)
        monkeypatch.setattr(backend, "close", record_backend_close)

        with pytest.raises(RuntimeError, match="kernel close exploded"):
            await runtime.close()

        assert closed == ["library.close", "flush", "backend.close"]
        assert runtime.closed
        assert any(diagnostic.kind == "runtime.close_failed" for diagnostic in received)

    asyncio.run(scenario())


def test_runtime_close_rejects_new_turns_and_stays_idempotent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )

        await runtime.close()
        await runtime.close()

        assert runtime.closed
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            async for _ in runtime.run_turn("session-a", UserMessage.text("late")):
                pass

    asyncio.run(scenario())


def test_runtime_close_cancels_active_turn_before_closing_backend(
    tmp_path: Path,
) -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, request):
            del request
            self.entered.set()
            await self.release.wait()
            yield _completion(AssistantMessage.text("late"))

    async def scenario() -> None:
        provider = BlockingProvider()
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
        )
        events: list[object] = []

        async def consume_turn() -> None:
            async for event in runtime.run_turn(
                "session-a",
                UserMessage.text("wait"),
            ):
                events.append(event)

        turn_task = asyncio.create_task(consume_turn())
        await provider.entered.wait()
        await asyncio.wait_for(runtime.close(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await turn_task

        assert events == []
        assert runtime.closed
        assert runtime._resources.backend._closed

    asyncio.run(scenario())


def test_runtime_close_from_turn_consumer_does_not_wait_on_itself(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(
                script=((_completion(AssistantMessage.text("done")),),)
            ),
            context_policy=_context_policy,
        )
        turn = runtime.run_turn("session-a", UserMessage.text("close"))

        assert isinstance(await anext(turn), ModelCompletion)
        await asyncio.wait_for(runtime.close(), timeout=1)
        with pytest.raises(StopAsyncIteration):
            await anext(turn)

        assert runtime.closed
        assert runtime._resources.backend._closed

    asyncio.run(scenario())


def test_flush_failure_is_visible_and_runtime_stays_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
            on_diagnostic=received.append,
        )

        async def fail_flush() -> None:
            raise RuntimeError("flush exploded")

        monkeypatch.setattr(runtime._resources.backend, "flush", fail_flush)

        with pytest.raises(RuntimeError, match="flush exploded"):
            await runtime.close()

        assert runtime.closed
        assert runtime._resources.backend._closed
        assert any(diagnostic.kind == "runtime.close_failed" for diagnostic in received)
        await runtime.close()

    asyncio.run(scenario())


def test_close_failure_is_visible_via_diagnostic(tmp_path: Path, monkeypatch) -> None:
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
            on_diagnostic=received.append,
        )

        async def fail_close() -> None:
            raise RuntimeError("workspace close exploded")

        monkeypatch.setattr(runtime._resources.backend, "close", fail_close)

        with pytest.raises(RuntimeError, match="workspace close exploded"):
            await runtime.close()

        failures = [
            diagnostic
            for diagnostic in received
            if diagnostic.kind == "runtime.close_failed"
        ]
        assert len(failures) == 1
        assert "workspace close exploded" in repr(failures[0])

    asyncio.run(scenario())


def test_runtime_close_terminates_running_executions(tmp_path: Path) -> None:
    async def scenario() -> None:
        call = ToolCall(
            call_id="long",
            name="exec",
            arguments={
                "command": f"{shlex.quote(sys.executable)} -c "
                f'"import time; time.sleep(30)"',
                "wait_ms": 0,
            },
        )
        provider = ScriptedModelProvider(
            script=(
                (
                    ToolCallReady(call=call),
                    ModelCompletion(
                        message=AssistantMessage(content=(call,)),
                        finish_reason="tool_calls",
                    ),
                ),
                (_completion(AssistantMessage.text("done")),),
            )
        )
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=provider,
            context_policy=_context_policy,
        )
        async for _ in runtime.run_turn("session-a", UserMessage.text("run long")):
            pass

        kernel = next(iter(runtime._sessions.values())).kernel
        state = next(iter(kernel._executions.values()))
        for _ in range(100):
            prepared = state.prepared_execution
            if prepared is not None and prepared._process._process is not None:
                break
            await asyncio.sleep(0.02)
        assert state.status == "running"

        await runtime.close()

        assert runtime.closed
        provider.assert_exhausted()

    asyncio.run(scenario())


def test_runtime_close_cancels_library_worker(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )
        catalog = runtime._resources.library_catalog
        assert catalog._worker_task is not None

        await runtime.close()

        assert catalog._worker_task is None
        assert runtime.closed

    asyncio.run(scenario())


def test_worker_start_failure_rolls_back_opened_resources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    opened: dict[str, object] = {}
    real_backend = resources_module._LocalBackend

    class _RecordingBackend(real_backend):
        async def open_workspace(
            self,
            source: object,
            capability_source: object,
            capability_state: object,
        ) -> object:
            workspace = await super().open_workspace(
                source, capability_source, capability_state
            )
            opened["workspace"] = workspace
            return workspace

    monkeypatch.setattr(resources_module, "_LocalBackend", _RecordingBackend)

    def fail_start(
        self: object, provider: object, on_diagnostic: object = None
    ) -> None:
        del self, provider, on_diagnostic
        raise RuntimeError("worker start exploded")

    monkeypatch.setattr(library_module._LibraryCatalog, "start", fail_start)

    with pytest.raises(RuntimeError, match="worker start exploded"):
        asyncio.run(
            AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )
        )

    assert opened["workspace"]._closed is True


def test_backend_open_failure_never_creates_local_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailingBackend:
        calls = 0

        async def open_workspace(
            self,
            source: object,
            capability_source: object,
            capability_state: object,
        ) -> object:
            del source, capability_source, capability_state
            type(self).calls += 1
            raise ValueError("backend constraint failed")

    monkeypatch.setattr(resources_module, "_LocalBackend", FailingBackend)

    with pytest.raises(ValueError, match="backend constraint failed"):
        asyncio.run(
            AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )
        )

    assert FailingBackend.calls == 1
