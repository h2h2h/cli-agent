"""CapabilityProvider: the logical capability control plane.

The Provider reads capability source (Workspace root, Repertoire lower
tree, and the Workspace state upper tree) and produces one immutable
CapabilitySnapshot aggregating Tools, Skills, MCP, and project
instructions metadata. Discovery never starts workers or processes,
never writes Workspace files, and never depends on a Backend,
BackendWorkspace, ExecutionHandle, or Tool worker; deploying the
snapshot is the Deployment plane's job.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from cli_agent.runtime._capability.mcp.catalog import render_stub, stub_filename
from cli_agent.runtime._capability.mcp.config import discover_configs
from cli_agent.runtime._capability.mcp.discovery import (
    MCPDiscovery,
    MCPDiscoveryEnvironment,
)
from cli_agent.runtime._capability.mcp.facts import MCPServerConfig, _MCPServerFacts
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.snapshot import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.source_view import (
    CapabilitySource,
    _RecordingCapabilitySource,
)
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._project_instructions import _ProjectInstructions
from cli_agent.runtime.host import NULL_EVENTS, EventSink


class ProjectInstructionsSource(Protocol):
    """Narrow source of validated project instruction facts."""

    async def load_project_instructions(self) -> _ProjectInstructions | None:
        """Load the effective project instructions, if present."""
        ...


class CapabilityProvider(Protocol):
    """Discover one immutable snapshot from control-plane inputs."""

    async def discover(
        self,
        source: CapabilitySource,
        *,
        mcp_discovery: MCPDiscovery,
        mcp_environment: MCPDiscoveryEnvironment,
        project_instructions: ProjectInstructionsSource,
    ) -> CapabilitySnapshot:
        """Return logical capability facts without deployment writes."""
        ...


class DefaultCapabilityProvider:
    """Default catalog-based implementation of ``CapabilityProvider``."""

    def __init__(
        self,
        *,
        events: EventSink = NULL_EVENTS,
    ) -> None:
        self._events = events

    async def discover(
        self,
        source: CapabilitySource,
        *,
        mcp_discovery: MCPDiscovery,
        mcp_environment: MCPDiscoveryEnvironment,
        project_instructions: ProjectInstructionsSource,
    ) -> CapabilitySnapshot:
        """Discover the complete snapshot without creating any resource.

        The returned snapshot covers Tools, Skills, MCP configs, and
        project instructions; the Deployment plane attaches the live
        Library Catalog afterwards through ``with_library``.

        MCP configuration, external discovery facts, catalogs, and project
        instructions are all consumed in one forward-only control-plane
        pass. The provider never observes deployment output.
        """

        source = _RecordingCapabilitySource(source)
        mcp_configs = await discover_configs(source, self._events)
        mcp_facts = await mcp_discovery.discover(
            mcp_configs,
            source,
            mcp_environment,
        )
        tools = await _ToolCatalog.discover(source, self._events)
        tools = tools.with_mcp(mcp_configs, mcp_facts)
        skills = await _SkillCatalog.discover(source)
        instructions = await project_instructions.load_project_instructions()
        inputs = source.fingerprint_inputs + _mcp_fingerprint_inputs(
            mcp_configs,
            mcp_facts,
        )
        if instructions is not None:
            inputs += (
                (
                    f"AGENTS.md@{instructions.source}",
                    instructions.text.encode("utf-8"),
                ),
            )
        return CapabilitySnapshot(
            revision=_snapshot_revision(inputs),
            schema_version=CAPABILITY_SCHEMA_VERSION,
            tools=tools,
            skills=skills,
            mcp_servers=mcp_configs,
            project_instructions=instructions,
            mcp_facts=mcp_facts,
        )


def _snapshot_revision(inputs: tuple[tuple[str, bytes], ...]) -> str:
    """Hash one snapshot's source inputs into a stable revision."""

    hasher = hashlib.sha256()
    for name, content in sorted(inputs, key=lambda item: item[0]):
        hasher.update(len(name.encode("utf-8")).to_bytes(8, "big"))
        hasher.update(name.encode("utf-8"))
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()


def _mcp_fingerprint_inputs(
    configs: tuple[MCPServerConfig, ...],
    facts: tuple[_MCPServerFacts, ...],
) -> tuple[tuple[str, bytes], ...]:
    by_server = {fact.name: fact for fact in facts}
    return tuple(
        (
            f"generated/{stub_filename(config.name)}",
            render_stub(config, by_server[config.name]).encode("utf-8"),
        )
        for config in configs
        if config.name in by_server
    )
