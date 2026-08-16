"""Typed Runtime composition inputs with no concrete Backend selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli_agent.runtime._capability.deployment import (
    CapabilityDeployment,
    ToolExecutorFactory,
)
from cli_agent.runtime._capability.mcp.discovery import MCPDiscovery
from cli_agent.runtime._capability.overlay import CapabilityOverlayFactory
from cli_agent.runtime._capability.provider import CapabilityProvider
from cli_agent.runtime._capability.source_view import CapabilitySourceFactory
from cli_agent.runtime._context import ContextEngineFactory
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime._environment.policy import ExecutionPolicy
from cli_agent.runtime._workspace import WorkspaceFactory
from cli_agent.runtime.host import HostServices


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Host-side facts used to open one logical Workspace."""

    root: str | Path
    repertoire: str | Path | None = None


@dataclass(frozen=True, slots=True)
class RuntimeComponents:
    """One-shot composition of replaceable Runtime ports and policies.

    AgentRuntime consumes this aggregate only while opening. No downstream
    object receives or stores the aggregate itself.
    """

    workspace_factory: WorkspaceFactory
    capability_source_factory: CapabilitySourceFactory
    capability_provider: CapabilityProvider
    mcp_discovery: MCPDiscovery
    capability_deployment: CapabilityDeployment
    capability_overlay_factory: CapabilityOverlayFactory
    tool_executor_factory: ToolExecutorFactory
    context_factory: ContextEngineFactory
    session_store: SessionStore
    summary_cache: _SummaryCache
    policy: ExecutionPolicy | None
    host: HostServices
