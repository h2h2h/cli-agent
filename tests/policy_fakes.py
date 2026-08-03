"""Test-local ExecutionPolicy fakes shared across Runtime contract tests."""

from __future__ import annotations

from cli_agent.runtime._capability.command_parser import (
    _DIRECT_MUTATORS,
    ShellParseResult,
)
from cli_agent.runtime._environment.policy import PolicyAction, PolicyEvaluation


class _AllowAllPolicy:
    """Allow every parsed command without inspecting it."""

    async def evaluate(self, command: ShellParseResult) -> PolicyEvaluation:
        del command
        return PolicyEvaluation(action=PolicyAction.ALLOW, rule_id="test.allow")


class _AskForWritesPolicy:
    """Ask for output redirection and direct mutator invocations."""

    async def evaluate(self, command: ShellParseResult) -> PolicyEvaluation:
        if (
            command.contains_output_redirection
            or command.executable_basename in _DIRECT_MUTATORS
        ):
            return PolicyEvaluation(
                action=PolicyAction.ASK,
                rule_id="test.ask-write",
                reason="write requires Host approval",
            )
        return PolicyEvaluation(action=PolicyAction.ALLOW, rule_id="test.allow")


class _DenyExecutablePolicy:
    """Deny direct invocations of the configured executable basenames."""

    def __init__(
        self,
        executables: frozenset[str],
        *,
        reason: str,
    ) -> None:
        self._executables = executables
        self._reason = reason

    async def evaluate(self, command: ShellParseResult) -> PolicyEvaluation:
        if command.executable_basename in self._executables:
            return PolicyEvaluation(
                action=PolicyAction.DENY,
                rule_id="test.deny-executable",
                reason=self._reason,
            )
        return PolicyEvaluation(action=PolicyAction.ALLOW, rule_id="test.allow")


class _AskExecutablePolicy:
    """Ask for direct invocations of the configured executable basenames."""

    def __init__(
        self,
        executables: frozenset[str],
        *,
        rule_id: str,
        reason: str,
    ) -> None:
        self._executables = executables
        self._rule_id = rule_id
        self._reason = reason

    async def evaluate(self, command: ShellParseResult) -> PolicyEvaluation:
        if command.executable_basename in self._executables:
            return PolicyEvaluation(
                action=PolicyAction.ASK,
                rule_id=self._rule_id,
                reason=self._reason,
            )
        return PolicyEvaluation(action=PolicyAction.ALLOW, rule_id="test.allow")
