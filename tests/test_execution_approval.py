import asyncio
import shlex
import sys
from pathlib import Path

import pytest
from policy_fakes import _AskExecutablePolicy

from cli_agent.runtime import (
    ApprovalResponse,
    ExecutionApprovalRequest,
    ToolCall,
    ToolResult,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.policy import _ExecutionApprovalGate


def test_host_approval_allows_one_exact_command(tmp_path: Path) -> None:
    approver = _RecordingApprover(ApprovalResponse.ALLOW)
    policy = _ask_for_python_policy()
    gate = _ExecutionApprovalGate(approver)
    proof = tmp_path / "approved"
    command = _python_command(f"from pathlib import Path; Path({str(proof)!r}).touch()")

    async def scenario() -> None:
        kernel = EnvironmentKernel(
            tmp_path,
            policy=policy,
            approval_gate=gate,
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="approved",
                    name="exec",
                    arguments={"command": command},
                )
            )

            snapshot = _output(result)
            assert snapshot["status"] == "exited"
            assert proof.exists()
            assert len(kernel._executions) == 1
            state = next(iter(kernel._executions.values()))
            request = approver.requests[0]
            assert state.command.raw_command == command
            assert request.session_id is None
            assert request.raw_command == command
            assert request.tokens == parse_shell_ast(command).tokens
            assert request.executable_basename == Path(sys.executable).name
            assert request.contains_shell_composition is False
            assert not hasattr(request, "tool")
            assert request.rule_id.startswith("shell.ask-executable.")
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_host_approval_denial_creates_no_execution(tmp_path: Path) -> None:
    approver = _RecordingApprover(ApprovalResponse.DENY)
    proof = tmp_path / "denied"

    async def scenario() -> None:
        kernel = EnvironmentKernel(
            tmp_path,
            policy=_ask_for_python_policy(),
            approval_gate=_ExecutionApprovalGate(approver),
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="denied",
                    name="exec",
                    arguments={"command": _touch_command(proof)},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": "execution approval was denied by the Host",
            }
            assert not proof.exists()
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("approver_kind", "expected_message"),
    (
        (
            "failing",
            "execution approver failed closed",
        ),
        (
            "invalid",
            "execution approver returned an invalid response",
        ),
    ),
)
def test_approval_callback_failures_create_no_execution(
    tmp_path: Path,
    approver_kind: str,
    expected_message: str,
) -> None:
    async def scenario() -> None:
        approver = (
            _FailingApprover() if approver_kind == "failing" else _InvalidApprover()
        )
        kernel = EnvironmentKernel(
            tmp_path,
            policy=_ask_for_python_policy(),
            approval_gate=_ExecutionApprovalGate(approver),  # type: ignore[arg-type]
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="failed-approval",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": expected_message,
            }
            assert kernel._executions == {}
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_approval_timeout_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        approver = _BlockingApprover()
        kernel = EnvironmentKernel(
            tmp_path,
            policy=_ask_for_python_policy(),
            approval_gate=_ExecutionApprovalGate(
                approver,
                timeout_seconds=0.01,
            ),
        )
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="timed-out-approval",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )

            assert _error(result) == {
                "ok": False,
                "code": "policy_denied",
                "message": "execution approval timed out",
            }
            assert kernel._executions == {}
        finally:
            approver.release.set()
            await kernel.close()

    asyncio.run(scenario())


def test_runtime_wide_approval_capacity_is_immediate_and_pre_admission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        approver = _BlockingApprover()
        gate = _ExecutionApprovalGate(
            approver,
            capacity=1,
        )
        policy = _ask_for_python_policy()
        first = EnvironmentKernel(
            tmp_path,
            policy=policy,
            approval_gate=gate,
        )
        second = EnvironmentKernel(
            tmp_path,
            policy=policy,
            approval_gate=gate,
        )
        first_task = asyncio.create_task(
            first.dispatch(
                ToolCall(
                    call_id="first-approval",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )
        )
        try:
            await approver.entered.wait()
            assert first._executions == {}

            overflow = await second.dispatch(
                ToolCall(
                    call_id="approval-overflow",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )

            assert _error(overflow) == {
                "ok": False,
                "code": "policy_denied",
                "message": "execution approval capacity is full",
            }
            assert second._executions == {}

            approver.release.set()
            approved = _output(await first_task)
            assert approved["status"] == "exited"
        finally:
            approver.release.set()
            if not first_task.done():
                await first_task
            await first.close()
            await second.close()

    asyncio.run(scenario())


def test_session_close_cancels_its_pending_approval(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        approver = _BlockingApprover()
        kernel = EnvironmentKernel(
            tmp_path,
            policy=_ask_for_python_policy(),
            approval_gate=_ExecutionApprovalGate(approver),
        )
        dispatch = asyncio.create_task(
            kernel.dispatch(
                ToolCall(
                    call_id="cancelled-approval",
                    name="exec",
                    arguments={"command": _python_command("pass")},
                )
            )
        )

        await approver.entered.wait()
        assert kernel._executions == {}
        await kernel.close()
        result = await asyncio.wait_for(dispatch, timeout=0.5)

        assert _error(result) == {
            "ok": False,
            "code": "internal",
            "message": "environment session is closed",
        }
        assert approver.cancelled.is_set()
        assert kernel._executions == {}

    asyncio.run(scenario())


class _RecordingApprover:
    def __init__(self, response: ApprovalResponse) -> None:
        self._response = response
        self.requests: list[ExecutionApprovalRequest] = []

    async def approve(
        self,
        request: ExecutionApprovalRequest,
    ) -> ApprovalResponse:
        self.requests.append(request)
        return self._response


class _FailingApprover:
    async def approve(
        self,
        request: ExecutionApprovalRequest,
    ) -> ApprovalResponse:
        raise RuntimeError(request.request_id)


class _InvalidApprover:
    async def approve(
        self,
        request: ExecutionApprovalRequest,
    ) -> object:
        return request.request_id


class _BlockingApprover:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def approve(
        self,
        request: ExecutionApprovalRequest,
    ) -> ApprovalResponse:
        del request
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ApprovalResponse.ALLOW


def _ask_for_python_policy() -> _AskExecutablePolicy:
    return _AskExecutablePolicy(
        frozenset({Path(sys.executable).name}),
        rule_id="shell.ask-executable.python",
        reason="direct invocation of python requires Host approval",
    )


def _touch_command(path: Path) -> str:
    return _python_command(f"from pathlib import Path; Path({str(path)!r}).touch()")


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _error(result: ToolResult) -> dict[str, object]:
    assert result.output is None
    assert isinstance(result.error, dict)
    return result.error
