"""Default Host-side Local and Docker Runtime component presets."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Coroutine

from cli_agent._adapters.docker.deployment import (
    _DockerCapabilityDeployment,
    _DockerCapabilityOverlayFactory,
    _DockerMCPDiscovery,
    _DockerToolExecutorFactory,
)
from cli_agent._adapters.local.deployment import _LocalCapabilityDeployment
from cli_agent._adapters.local.executor import _LocalToolExecutorFactory
from cli_agent._adapters.local.mcp_discovery import _LocalMCPDiscovery
from cli_agent._adapters.local.overlay import _LocalCapabilityOverlayFactory
from cli_agent._workspaces import _DockerWorkspaceFactory, _LocalWorkspaceFactory
from cli_agent.runtime._capability.provider import DefaultCapabilityProvider
from cli_agent.runtime._capability.source_view import (
    _HostCapabilitySourceFactory,
    _WorkspaceCapabilitySourceFactory,
)
from cli_agent.runtime._composition import RuntimeComponents, WorkspaceConfig
from cli_agent.runtime._context import ContextEngineFactory, ContextPolicy
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime._environment.interaction import UserInteraction
from cli_agent.runtime._environment.policy import ExecutionPolicy
from cli_agent.runtime.host import EventSink, HostServices
from cli_agent.runtime.model import ModelProvider
from cli_agent.runtime.runtime import AgentRuntime


def local_runtime_components(
    *,
    interaction: UserInteraction,
    context_policy: ContextPolicy,
    policy: ExecutionPolicy | None = None,
    events: EventSink | None = None,
    state_path: str | Path | None = None,
) -> RuntimeComponents:
    """Build the default Local component set outside AgentRuntime."""

    host = HostServices(interaction=interaction, events=events)
    database = _StateDatabase.open(state_path)
    session_store = SessionStore(database)
    return RuntimeComponents(
        workspace_factory=_LocalWorkspaceFactory(),
        capability_source_factory=_HostCapabilitySourceFactory(),
        capability_provider=DefaultCapabilityProvider(events=host.events),
        mcp_discovery=_LocalMCPDiscovery(host.events),
        capability_deployment=_LocalCapabilityDeployment(
            events=host.events,
        ),
        capability_overlay_factory=_LocalCapabilityOverlayFactory(),
        tool_executor_factory=_LocalToolExecutorFactory(),
        context_factory=ContextEngineFactory(
            store=session_store,
            context_policy=context_policy,
            events=host.events,
        ),
        session_store=session_store,
        summary_cache=_SummaryCache(database),
        policy=policy,
        host=host,
    )


def docker_runtime_components(
    *,
    interaction: UserInteraction,
    context_policy: ContextPolicy,
    policy: ExecutionPolicy | None = None,
    events: EventSink | None = None,
    state_path: str | Path | None = None,
) -> RuntimeComponents:
    """Build the default Docker component set outside AgentRuntime."""

    host = HostServices(interaction=interaction, events=events)
    database = _StateDatabase.open(state_path)
    session_store = SessionStore(database)
    return RuntimeComponents(
        workspace_factory=_DockerWorkspaceFactory(),
        capability_source_factory=_WorkspaceCapabilitySourceFactory(),
        capability_provider=DefaultCapabilityProvider(events=host.events),
        mcp_discovery=_DockerMCPDiscovery(host.events),
        capability_deployment=_DockerCapabilityDeployment(
            events=host.events,
        ),
        capability_overlay_factory=_DockerCapabilityOverlayFactory(),
        tool_executor_factory=_DockerToolExecutorFactory(),
        context_factory=ContextEngineFactory(
            store=session_store,
            context_policy=context_policy,
            events=host.events,
        ),
        session_store=session_store,
        summary_cache=_SummaryCache(database),
        policy=policy,
        host=host,
    )


def default_runtime_components(
    backend: str,
    **kwargs: object,
) -> RuntimeComponents:
    """Select a default preset at the Host boundary."""

    if backend == "local":
        return local_runtime_components(**kwargs)  # type: ignore[arg-type]
    if backend == "docker":
        return docker_runtime_components(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unsupported Backend kind: {backend}")


def open_default_runtime(
    *,
    workspace: str | Path,
    provider: ModelProvider,
    interaction: UserInteraction,
    context_policy: ContextPolicy,
    repertoire: str | Path | None = None,
    policy: ExecutionPolicy | None = None,
    events: EventSink | None = None,
    backend: str = "local",
    state_path: str | Path | None = None,
    system_instruction: str | None = None,
    parallel_commands: frozenset[str] | None = None,
) -> Coroutine[Any, None, AgentRuntime]:
    """Open AgentRuntime through an explicit Host-selected default preset."""

    components_factory = (
        local_runtime_components if backend == "local" else docker_runtime_components
    )
    if backend not in {"local", "docker"}:
        raise ValueError(f"unsupported Backend kind: {backend}")
    components = components_factory(
        interaction=interaction,
        context_policy=context_policy,
        policy=policy,
        events=events,
        state_path=state_path,
    )
    return AgentRuntime.open(
        provider=provider,
        components=components,
        workspace_config=WorkspaceConfig(root=workspace, repertoire=repertoire),
        system_instruction=system_instruction,
        parallel_commands=parallel_commands,
    )
