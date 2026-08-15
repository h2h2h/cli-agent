"""Resolve parsed commands to one ExecutionSource and its schedule fact."""

from __future__ import annotations

from dataclasses import dataclass

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.sources import (
    ExecutionSource,
    _SourceRegistry,
)


@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    """Bind one parsed command to its selected Source and schedule fact."""

    source: ExecutionSource
    parallel_safe: bool

    def __post_init__(self) -> None:
        if not isinstance(self.parallel_safe, bool):
            raise TypeError("execution route parallel_safe must be a bool")


class _CommandRouter:
    """Resolve registered command heads and otherwise use the Shell fallback."""

    def __init__(
        self,
        *,
        shell_source: ExecutionSource,
        sources: _SourceRegistry,
    ) -> None:
        self._shell_source = shell_source
        self._sources = sources

    def resolve(self, command: ShellParseResult) -> _ExecutionRoute:
        """Select one Source and its schedule fact without performing work."""

        source = self._sources.resolve(command)
        if source is None:
            source = self._shell_source

        return _ExecutionRoute(
            source=source,
            parallel_safe=source.parallel_safe(command),
        )
