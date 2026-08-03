"""Host-owned execution policy contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

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
