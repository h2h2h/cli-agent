"""Host-owned execution policy and approval contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from cli_agent.runtime._capability.command_parser import (
    _DIRECT_MUTATORS,
    CommandParseResult,
    ToolCommand,
    _sed_is_in_place,
)


class PolicyAction(str, Enum):
    """One read-only Host policy evaluation action."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """An allow, deny, or ask evaluation for one exact parsed command."""

    action: PolicyAction
    parse_result: CommandParseResult
    rule_id: str
    reason: str | None = None

    @classmethod
    def allow(
        cls,
        parse_result: CommandParseResult,
        *,
        rule_id: str = "default.allow",
    ) -> PolicyEvaluation:
        return cls(
            action=PolicyAction.ALLOW,
            parse_result=parse_result,
            rule_id=rule_id,
        )

    @classmethod
    def deny(
        cls,
        parse_result: CommandParseResult,
        *,
        rule_id: str,
        reason: str,
    ) -> PolicyEvaluation:
        return cls(
            action=PolicyAction.DENY,
            parse_result=parse_result,
            rule_id=rule_id,
            reason=reason,
        )

    @classmethod
    def ask(
        cls,
        parse_result: CommandParseResult,
        *,
        rule_id: str,
        reason: str,
    ) -> PolicyEvaluation:
        return cls(
            action=PolicyAction.ASK,
            parse_result=parse_result,
            rule_id=rule_id,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """Final immutable authorization for one exact parsed command."""

    parse_result: CommandParseResult
    rule_id: str
    approval_request_id: str | None = None

    @classmethod
    def allow(
        cls,
        parse_result: CommandParseResult,
        *,
        rule_id: str = "default.allow",
        approval_request_id: str | None = None,
    ) -> ExecutionDecision:
        return cls(
            parse_result=parse_result,
            rule_id=rule_id,
            approval_request_id=approval_request_id,
        )


class ExecutionPolicy(Protocol):
    """Host-owned Policy shared by one Runtime's Session Kernels."""

    async def evaluate(
        self,
        command: CommandParseResult,
    ) -> PolicyEvaluation:
        """Evaluate one parsed command without performing its operation."""


class ExecutablePolicy:
    """Evaluate direct executable names using disjoint Host-owned sets."""

    def __init__(
        self,
        *,
        allowed_executables: frozenset[str] = frozenset(),
        denied_executables: frozenset[str] = frozenset(),
        asked_executables: frozenset[str] = _DIRECT_MUTATORS,
        default_action: PolicyAction = PolicyAction.ALLOW,
    ) -> None:
        if not isinstance(default_action, PolicyAction):
            raise ValueError("default policy action must be a PolicyAction")

        configured = {
            PolicyAction.ALLOW: frozenset(allowed_executables),
            PolicyAction.DENY: frozenset(denied_executables),
            PolicyAction.ASK: frozenset(asked_executables),
        }
        for action, names in configured.items():
            invalid = sorted(
                name
                for name in names
                if not name or Path(name).name != name
            )
            if invalid:
                raise ValueError(
                    f"{action.value} executable names must be non-empty path basenames"
                )

        overlaps = (
            configured[PolicyAction.ALLOW] & configured[PolicyAction.DENY]
        ) | (
            configured[PolicyAction.ALLOW] & configured[PolicyAction.ASK]
        ) | (
            configured[PolicyAction.DENY] & configured[PolicyAction.ASK]
        )
        if overlaps:
            raise ValueError("executable policy sets must be disjoint")

        self._by_action = configured
        self._default_action = default_action

    async def evaluate(
        self,
        command: CommandParseResult,
    ) -> PolicyEvaluation:
        executable = command.executable_basename
        action = self._default_action
        matched = False
        if executable is not None:
            for candidate, names in self._by_action.items():
                if executable in names:
                    action = candidate
                    matched = True
                    break

        if command.tool is not None and not matched:
            return PolicyEvaluation.allow(
                command,
                rule_id=f"tool.{command.tool.operation}.allow",
            )

        if not matched and command.contains_output_redirection:
            return PolicyEvaluation.ask(
                command,
                rule_id="shell.ask-output-redirection",
                reason="Shell output redirection requires Host approval",
            )
        if (
            not matched
            and executable == "sed"
            and _sed_is_in_place(command.tokens[1:])
        ):
            return PolicyEvaluation.ask(
                command,
                rule_id="shell.ask-in-place-edit",
                reason="in-place file editing requires Host approval",
            )

        rule_id = (
            f"shell.{action.value}-executable.{executable}"
            if matched
            else f"default.{action.value}"
        )
        if action is PolicyAction.ALLOW:
            return PolicyEvaluation.allow(command, rule_id=rule_id)

        if matched:
            reason = (
                f"direct invocation of {executable!r} is denied by policy"
                if action is PolicyAction.DENY
                else f"direct invocation of {executable!r} requires Host approval"
            )
        else:
            reason = (
                "execution is denied by the default policy"
                if action is PolicyAction.DENY
                else "execution requires Host approval by the default policy"
            )
        if action is PolicyAction.DENY:
            return PolicyEvaluation.deny(
                command,
                rule_id=rule_id,
                reason=reason,
            )
        return PolicyEvaluation.ask(
            command,
            rule_id=rule_id,
            reason=reason,
        )


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
    tool: ToolCommand | None
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
        *,
        session_id: str | None = None,
    ) -> _ApprovalResolution:
        if evaluation.action is not PolicyAction.ASK or evaluation.reason is None:
            raise RuntimeError("approval gate received a non-ASK evaluation")

        async with self._lock:
            if self._active >= self._capacity:
                raise _ApprovalResolutionError(
                    "execution approval capacity is full"
                )
            self._active += 1

        request = ExecutionApprovalRequest(
            request_id=uuid4().hex,
            session_id=session_id,
            raw_command=evaluation.parse_result.raw_command,
            tokens=evaluation.parse_result.tokens,
            executable_basename=evaluation.parse_result.executable_basename,
            tokenization_succeeded=(
                evaluation.parse_result.tokenization_succeeded
            ),
            contains_shell_composition=(
                evaluation.parse_result.contains_shell_composition
            ),
            contains_output_redirection=(
                evaluation.parse_result.contains_output_redirection
            ),
            tool=evaluation.parse_result.tool,
            rule_id=evaluation.rule_id,
            reason=evaluation.reason,
        )
        try:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    response = await self._approver.approve(request)
            except TimeoutError as exc:
                raise _ApprovalResolutionError(
                    "execution approval timed out"
                ) from exc
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
