"""Host-owned execution policy and approval contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import uuid4

from cli_agent.runtime._capability.command_parser import ShellParseResult


class PolicyAction(str, Enum):
    """One read-only Host policy evaluation action."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """An allow, deny, or ask conclusion for one exact parsed command."""

    action: PolicyAction
    rule_id: str
    reason: str | None = None


class ExecutionPolicy(Protocol):
    """Host-owned Policy shared by one Runtime's Session Kernels."""

    async def evaluate(
        self,
        command: ShellParseResult,
    ) -> PolicyEvaluation:
        """Evaluate one parsed command without performing its operation."""


@dataclass(frozen=True, slots=True)
class ExecutionApprovalRequest:
    """Host-visible facts for one Policy ASK evaluation."""

    request_id: str
    session_id: str | None
    raw_command: str
    tokens: tuple[str, ...]
    executable_basename: str | None
    tokenization_succeeded: bool
    contains_shell_composition: bool
    contains_output_redirection: bool
    rule_id: str
    reason: str


class ApprovalResponse(str, Enum):
    """One allow-once or deny response from a Host approver."""

    ALLOW = "allow"
    DENY = "deny"


class ExecutionApprover(Protocol):
    """Host callback for resolving one execution approval request."""

    async def approve(
        self,
        request: ExecutionApprovalRequest,
    ) -> ApprovalResponse:
        """Return an allow-once or deny response."""


@dataclass(frozen=True, slots=True)
class _ApprovalResolution:
    request_id: str
    response: ApprovalResponse


class _ApprovalResolutionError(RuntimeError):
    """Safe approval failure suitable for a model-visible denial."""


class _ExecutionApprovalGate:
    """Bound Runtime-wide Host approval calls without creating Executions."""

    def __init__(
        self,
        approver: ExecutionApprover,
        *,
        capacity: int = 8,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._approver = approver
        self._capacity = capacity
        self._timeout_seconds = timeout_seconds
        self._active = 0
        self._lock = asyncio.Lock()

    async def request(
        self,
        evaluation: PolicyEvaluation,
        command: ShellParseResult,
        *,
        session_id: str | None = None,
    ) -> _ApprovalResolution:
        if evaluation.action is not PolicyAction.ASK or evaluation.reason is None:
            raise RuntimeError("approval gate received a non-ASK evaluation")

        async with self._lock:
            if self._active >= self._capacity:
                raise _ApprovalResolutionError("execution approval capacity is full")
            self._active += 1

        request = ExecutionApprovalRequest(
            request_id=uuid4().hex,
            session_id=session_id,
            raw_command=command.raw_command,
            tokens=command.tokens,
            executable_basename=command.executable_basename,
            tokenization_succeeded=command.tokenization_succeeded,
            contains_shell_composition=command.contains_shell_composition,
            contains_output_redirection=command.contains_output_redirection,
            rule_id=evaluation.rule_id,
            reason=evaluation.reason,
        )
        try:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    response = await self._approver.approve(request)
            except TimeoutError as exc:
                raise _ApprovalResolutionError("execution approval timed out") from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _ApprovalResolutionError(
                    "execution approver failed closed"
                ) from exc

            if not isinstance(response, ApprovalResponse):
                raise _ApprovalResolutionError(
                    "execution approver returned an invalid response"
                )
            return _ApprovalResolution(
                request_id=request.request_id,
                response=response,
            )
        finally:
            async with self._lock:
                self._active -= 1
