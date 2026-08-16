"""Unit tests for the consumer-facing error boundary taxonomy."""

import asyncio
import json
from pathlib import Path

import pytest
from host_fakes import _environment_kernel
from interaction_fakes import _ScriptedInteraction
from workspace_fakes import _kernel_workspace

from cli_agent.errors import (
    INTERNAL_ERROR_DIAGNOSTIC_KIND,
    CliAgentError,
    ContextExhaustedError,
    HostFacingError,
    InternalRuntimeError,
    ModelFacingError,
    error_boundary,
    internal_from_exception,
)
from cli_agent.presets import open_default_runtime
from cli_agent.runtime import (
    AgentRuntime,
    CallbackEventSink,
    ContextPolicy,
    ModelRequest,
    RuntimeDiagnostic,
    ToolCall,
    ToolResult,
    UserMessage,
)
from cli_agent.runtime.model import ModelContextOverflowSignal

_context_policy = ContextPolicy(
    context_window_tokens=128_000,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
    minimum_reclaim_tokens=1,
)
_user_interaction = _ScriptedInteraction("allow_once")


def test_classified_errors_share_one_taxonomy() -> None:
    assert issubclass(ModelFacingError, CliAgentError)
    assert issubclass(HostFacingError, CliAgentError)
    assert issubclass(InternalRuntimeError, HostFacingError)
    assert issubclass(CliAgentError, Exception)


def test_model_facing_payload_is_stable() -> None:
    error = ModelFacingError(
        "unknown_execution",
        "Execution 'abc' does not exist.",
        retryable=True,
        details={"exec_id": "abc"},
    )

    assert error.code == "unknown_execution"
    assert error.message == "Execution 'abc' does not exist."
    assert error.retryable is True
    assert error.to_payload() == {
        "code": "unknown_execution",
        "message": "Execution 'abc' does not exist.",
        "retryable": True,
        "details": {"exec_id": "abc"},
    }
    json.dumps(error.to_payload())


def test_model_facing_payload_defaults() -> None:
    error = ModelFacingError("invalid_argument", "bad argument")

    assert error.retryable is False
    assert error.to_payload() == {
        "code": "invalid_argument",
        "message": "bad argument",
        "retryable": False,
        "details": {},
    }


def test_host_facing_payload_is_stable() -> None:
    error = HostFacingError(
        "backend_unavailable",
        "Unable to open the execution backend.",
        hint="Check whether the backend configuration is valid.",
        details={"workspace_id": "ws_1"},
    )

    assert error.code == "backend_unavailable"
    assert error.hint == "Check whether the backend configuration is valid."
    assert error.to_payload() == {
        "code": "backend_unavailable",
        "message": "Unable to open the execution backend.",
        "hint": "Check whether the backend configuration is valid.",
        "details": {"workspace_id": "ws_1"},
    }
    json.dumps(error.to_payload())


def test_host_facing_payload_defaults() -> None:
    error = HostFacingError("session_persistence_failed", "journal write failed")

    assert error.hint is None
    assert error.to_payload() == {
        "code": "session_persistence_failed",
        "message": "journal write failed",
        "hint": None,
        "details": {},
    }


def test_error_boundary_retains_classified_errors() -> None:
    diagnostics: list[tuple[str, str, dict[str, object]]] = []
    original = HostFacingError("workspace_mismatch", "session belongs elsewhere")

    with pytest.raises(HostFacingError) as raised:
        with error_boundary(
            "runtime.run_turn",
            sink=lambda *item: diagnostics.append(item),
        ):
            raise original

    assert raised.value is original
    assert diagnostics == []


def test_error_boundary_retains_cancellation() -> None:
    diagnostics: list[tuple[str, str, dict[str, object]]] = []

    with pytest.raises(asyncio.CancelledError):
        with error_boundary(
            "kernel.dispatch",
            sink=lambda *item: diagnostics.append(item),
        ):
            raise asyncio.CancelledError

    assert diagnostics == []


def test_error_boundary_retains_declared_passthrough() -> None:
    diagnostics: list[tuple[str, str, dict[str, object]]] = []

    class LegacyTurnError(RuntimeError):
        pass

    with pytest.raises(LegacyTurnError):
        with error_boundary(
            "runtime.run_turn",
            sink=lambda *item: diagnostics.append(item),
            passthrough=(LegacyTurnError,),
        ):
            raise LegacyTurnError("cannot be recovered safely")

    assert diagnostics == []


def test_error_boundary_returns_values_untouched() -> None:
    result = ToolResult(call_id="c1", output={"ok": True})

    with error_boundary("kernel.dispatch"):
        value = result

    assert value is result


def test_error_boundary_wraps_unexpected_exceptions() -> None:
    diagnostics: list[tuple[str, str, dict[str, object]]] = []

    with pytest.raises(InternalRuntimeError) as raised:
        with error_boundary(
            "kernel.dispatch",
            sink=lambda *item: diagnostics.append(item),
        ):
            raise KeyError("secret-token")

    error = raised.value
    assert error.code == "internal_error"
    assert error.details == {
        "operation": "kernel.dispatch",
        "exception_type": "KeyError",
    }
    assert error.__cause__ is not None
    payload = json.dumps(error.to_payload())
    assert "secret-token" not in payload
    assert "secret-token" not in error.message
    assert diagnostics == [
        (
            INTERNAL_ERROR_DIAGNOSTIC_KIND,
            "kernel.dispatch raised an unexpected exception",
            {
                "operation": "kernel.dispatch",
                "exception_type": "KeyError",
                "exception": repr(KeyError("secret-token")),
            },
        )
    ]
    assert "secret-token" in diagnostics[0][2]["exception"]


def test_error_boundary_works_without_diagnostic_sink() -> None:
    with pytest.raises(InternalRuntimeError) as raised:
        with error_boundary("runtime.run_turn"):
            raise TypeError("bug")

    assert raised.value.details["exception_type"] == "TypeError"


def test_internal_from_exception_never_embeds_the_original() -> None:
    original = ValueError("host path /etc/credential leaked")

    error = internal_from_exception(original, operation="runtime.run_turn")

    assert isinstance(error, InternalRuntimeError)
    payload = json.dumps(error.to_payload())
    assert "/etc/credential" not in payload
    assert set(error.details) == {"operation", "exception_type"}
    assert all(isinstance(value, str) for value in error.details.values())


def test_kernel_boundary_wraps_unexpected_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[RuntimeDiagnostic] = []

    def explode(command: object) -> None:
        del command
        raise KeyError("router bug")

    async def scenario() -> None:
        kernel = _environment_kernel(
            _kernel_workspace(tmp_path),
            events=CallbackEventSink(received.append),
        )
        monkeypatch.setattr(kernel._router, "resolve", explode)
        try:
            with pytest.raises(InternalRuntimeError) as raised:
                await kernel.dispatch(
                    ToolCall(
                        call_id="call-1",
                        name="exec",
                        arguments={"command": "echo hi", "wait_ms": 0},
                    )
                )
        finally:
            await kernel.close()

        error = raised.value
        assert error.details == {
            "operation": "kernel.dispatch",
            "exception_type": "KeyError",
        }
        assert [diagnostic.kind for diagnostic in received] == [
            INTERNAL_ERROR_DIAGNOSTIC_KIND
        ]
        assert "router bug" in str(received[0].detail["exception"])

    asyncio.run(scenario())


def test_kernel_boundary_keeps_expected_failures_as_data(
    tmp_path: Path,
) -> None:
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        kernel = _environment_kernel(_kernel_workspace(tmp_path), events=received.append)
        try:
            unknown = await kernel.dispatch(
                ToolCall(call_id="call-1", name="bogus", arguments={})
            )
            failed = await kernel.dispatch(
                ToolCall(
                    call_id="call-2",
                    name="exec",
                    arguments={"command": "sh -c 'exit 3'", "wait_ms": 8_000},
                )
            )
        finally:
            await kernel.close()

        assert unknown.error == {
            "ok": False,
            "code": "invalid_argument",
            "message": "unknown syscall: bogus",
        }
        assert failed.error is None
        output = failed.output
        assert isinstance(output, dict)
        assert output["status"] == "failed"
        assert output["exit_code"] == 3
        assert received == []

    asyncio.run(scenario())


class _ExplodingProvider:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def generate(self, request: ModelRequest):
        del request
        raise self._error
        yield  # pragma: no cover


class _OverflowTwiceProvider:
    def __init__(self) -> None:
        self._failures_left = 2

    async def generate(self, request: ModelRequest):
        del request
        if self._failures_left > 0:
            self._failures_left -= 1
            raise ModelContextOverflowSignal("provider context overflow")
        yield  # pragma: no cover


def _collect_turn(runtime: AgentRuntime, message: str) -> list[object]:
    async def scenario() -> list[object]:
        events: list[object] = []
        if runtime._binding is None:
            await runtime.new_session()
        async for event in runtime.run_turn(UserMessage.text(message)):
            events.append(event)
        return events

    return asyncio.run(scenario())


def test_run_turn_boundary_wraps_unexpected_exceptions(tmp_path: Path) -> None:
    received: list[RuntimeDiagnostic] = []

    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=_ExplodingProvider(RuntimeError("provider exploded")),
            interaction=_user_interaction,
            context_policy=_context_policy,
            events=CallbackEventSink(received.append),
        )
        try:
            with pytest.raises(InternalRuntimeError) as raised:
                await runtime.new_session()
                async for _ in runtime.run_turn(UserMessage.text("Hi")):
                    pass
        finally:
            await runtime.close()

        error = raised.value
        assert error.details == {
            "operation": "runtime.run_turn",
            "exception_type": "RuntimeError",
        }
        assert "provider exploded" not in error.message
        assert [diagnostic.kind for diagnostic in received] == [
            INTERNAL_ERROR_DIAGNOSTIC_KIND
        ]
        assert "provider exploded" in str(received[0].detail["exception"])

    asyncio.run(scenario())


def test_run_turn_classifies_second_overflow_as_host_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await open_default_runtime(
            workspace=tmp_path,
            provider=_OverflowTwiceProvider(),
            interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            session = await runtime.new_session()
            with pytest.raises(ContextExhaustedError) as raised:
                async for _ in runtime.run_turn(UserMessage.text("Hi")):
                    pass
        finally:
            await runtime.close()

        assert raised.value.code == "context_exhausted"
        assert raised.value.details["session_id"] == session.session_id

    asyncio.run(scenario())
