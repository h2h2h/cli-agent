"""Capability mutation overlay contract between Sources and deployment views."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._execution import ExecutionHandle


@runtime_checkable
class CapabilityOverlay(Protocol):
    """Delay capability copy-up/whiteout preparation until execution run."""

    def wrap_file(self, path: str, execution: ExecutionHandle) -> ExecutionHandle:
        """Wrap one direct file mutation for execution-time preparation."""
        ...

    def wrap_shell(
        self,
        command: ShellParseResult,
        cwd: str,
        execution: ExecutionHandle,
    ) -> ExecutionHandle:
        """Wrap one shell mutation for execution-time preparation."""
        ...

    async def close(self) -> None:
        """Close overlay-owned materialized state idempotently."""
        ...


class CapabilityOverlayFactory(Protocol):
    """Materialize one execution overlay after logical snapshot discovery."""

    async def create(self, workspace: object) -> CapabilityOverlay:
        """Create the overlay without exposing it to Backend state."""
        ...
