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
        """Select one Source and its schedule fact without performing work.

        Routing order:

        1. Look up the parsed command head in the Source Registry. The
           registry holds the built-in reserved heads (``cd``, ``export``,
           ``files``, ``tools``) plus any custom sources registered by the
           host; matching is an exact head-to-name lookup, so a custom
           command head always wins over the Shell fallback.
        2. If the head is not registered (``None``), fall back to the Shell
           Source: every unregistered command, including unknown or
           bare-executable commands, is executed as an ordinary shell
           command through the Workspace.
        3. Compute the selected Source's ``parallel_safe`` scheduling fact
           for this exact command and bundle it with the Source into an
           immutable ``_ExecutionRoute``. The scheduler later reuses this
           precomputed fact without re-parsing or re-evaluating the command.

        No execution work happens here; resolution only picks the handler
        family and its schedule fact.
        """

        source = self._sources.resolve(command)
        if source is None:
            source = self._shell_source

        return _ExecutionRoute(
            source=source,
            parallel_safe=source.parallel_safe(command),
        )
