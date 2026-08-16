"""Local adapter for the MCPDiscovery port."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from cli_agent._adapters.local.mcp_runtime import discover_servers
from cli_agent.runtime._capability.mcp.discovery import (
    MCPDiscoveryEnvironment,
    read_mcp_source,
)
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    _MCPServerFacts,
)
from cli_agent.runtime.host import NULL_EVENTS, EventSink

if TYPE_CHECKING:
    from cli_agent.runtime._capability.source_view import CapabilitySource


class _LocalMCPDiscovery:
    """Discover MCP facts using the Local execution environment."""

    def __init__(
        self,
        events: EventSink = NULL_EVENTS,
    ) -> None:
        self._events = events

    async def discover(
        self,
        configs: tuple[MCPServerConfig, ...],
        source: CapabilitySource,
        environment: MCPDiscoveryEnvironment,
    ) -> tuple[_MCPServerFacts, ...]:
        if not configs:
            return ()
        files = await read_mcp_source(source)
        with TemporaryDirectory(prefix="cli-agent-mcp-discovery-") as temporary:
            root = Path(temporary)
            for relative, content in files.items():
                target = root / ".workspace" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            staged = tuple(
                _rewrite_workspace_mcp_path(config, environment.root, root)
                for config in configs
            )
            return await discover_servers(
                staged,
                environment.execution_base_environment(),
                self._events,
            )


def _rewrite_workspace_mcp_path(
    config: MCPServerConfig,
    workspace_root: str,
    staged_root: Path,
) -> MCPServerConfig:
    command = config.command
    if command is None:
        return config
    source_prefix = str(Path(workspace_root) / ".workspace" / "_mcp")
    staged_prefix = str(staged_root / ".workspace" / "_mcp")
    relative_prefix = ".workspace/_mcp"

    def rewrite(argument: str) -> str:
        if argument == source_prefix or argument.startswith(source_prefix + "/"):
            return staged_prefix + argument[len(source_prefix) :]
        if argument == relative_prefix or argument.startswith(relative_prefix + "/"):
            return staged_prefix + argument[len(relative_prefix) :]
        return argument

    return replace(config, command=tuple(rewrite(part) for part in command))
