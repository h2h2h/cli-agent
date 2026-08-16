"""Backend-specific, read-only MCP discovery port."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    _MCPServerFacts,
)

if TYPE_CHECKING:
    from cli_agent.runtime._capability.source_view import CapabilitySource


class MCPDiscoveryEnvironment(Protocol):
    """Narrow execution facts required by an MCP discovery adapter."""

    @property
    def root(self) -> str:
        """Return the Backend-native Workspace root."""
        ...

    @property
    def backend(self) -> object:
        """Return the opaque Backend binding for adapter-specific use."""
        ...

    def execution_base_environment(self) -> Mapping[str, str]:
        """Return the child-process base environment."""
        ...


class MCPDiscovery(Protocol):
    """Discover logical MCP facts in a Workspace execution environment."""

    async def discover(
        self,
        configs: tuple[MCPServerConfig, ...],
        source: CapabilitySource,
        environment: MCPDiscoveryEnvironment,
    ) -> tuple[_MCPServerFacts, ...]:
        """Return provider-neutral facts without deployment writes."""
        ...


async def read_mcp_source(source: CapabilitySource) -> dict[str, bytes]:
    """Read the effective MCP source tree without materializing a view."""

    files: dict[str, bytes] = {}
    await _read_directory(source, "_mcp", files)
    return files


async def _read_directory(
    source: CapabilitySource,
    directory: str,
    files: dict[str, bytes],
) -> None:
    for entry in await source.list(directory):
        path = posixpath.join(directory, entry.name)
        if entry.metadata.kind == "directory":
            await _read_directory(source, path, files)
        else:
            files[path] = await source.read(path)
