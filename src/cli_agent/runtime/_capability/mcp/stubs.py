"""Shared MCP stub projection and invocation binding materialization.

The deployment plane materializes generated stubs and the Runtime-owned
invocation binding through the Workspace filesystem, independent of the
Backend: unchanged stub and binding domains are skipped via the completion
manifest, and a binding materialization failure keeps the previous
deployment in place. Both the Local and the Docker deployment call this
shared projection so their placement semantics cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping

from cli_agent.runtime._backend import _WorkspaceFilesystem
from cli_agent.runtime._capability.deployment import (
    TOOL_RUNTIME_DIRECTORY,
    _DeploymentManifest,
    artifact_digest,
    domains_match,
    publish_artifacts,
    volume_path,
)
from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.mcp.catalog import (
    MCP_STUB_PREFIX,
    render_stub,
    stub_filename,
)
from cli_agent.runtime._capability.mcp.facts import MCPServerConfig, _MCPServerFacts
from cli_agent.runtime._capability.mcp_binding import binding_filename, render_binding
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.host import NULL_EVENTS, EventSink, emit_event


async def materialize_stubs(
    *,
    filesystem: _WorkspaceFilesystem,
    volume: str,
    workspace_id: str,
    configs: tuple[MCPServerConfig, ...],
    facts: tuple[_MCPServerFacts, ...],
    manifest: _DeploymentManifest | None,
    realized: Mapping[str, str],
    events: EventSink = NULL_EVENTS,
) -> tuple[dict[str, str], bool]:
    """Project generated MCP stubs and the invocation binding.

    Unchanged stub and binding domains are skipped via the completion
    manifest. A binding materialization failure keeps the previous
    deployment in place, emits one ``mcp.binding_failed`` diagnostic, and
    skips stub projection for this round.

    Returns:
        The updated realized digest map and whether any domain changed.
    """

    discovered = {fact.name: fact for fact in facts}
    binding_configs = tuple(
        config for config in configs if config.name in discovered
    )
    stubs = {
        volume_path(volume, "tools", stub_filename(config.name)): render_stub(
            config,
            discovered[config.name],
        ).encode("utf-8")
        for config in binding_configs
    }
    binding_path = volume_path(volume, TOOL_RUNTIME_DIRECTORY, binding_filename())
    binding = render_binding(binding_configs).encode("utf-8")
    desired = {
        "stubs": artifact_digest(stubs),
        "binding": artifact_digest({binding_path: binding}),
    }
    if domains_match(
        manifest,
        workspace_id=workspace_id,
        digests=desired,
    ):
        return dict(realized), False
    await remove_stale_stubs(filesystem, volume, keep=frozenset(stubs))
    try:
        await publish_artifacts(
            filesystem,
            {binding_path: binding},
        )
    except Exception as exc:
        _emit(
            events,
            "mcp.binding_failed",
            "MCP invocation binding could not be materialized",
            {"error": str(exc)},
        )
        return dict(realized), False
    await publish_artifacts(filesystem, stubs)
    updated = dict(realized)
    updated.update(desired)
    return updated, True


async def remove_stale_stubs(
    filesystem: _WorkspaceFilesystem,
    volume: str,
    *,
    keep: frozenset[str],
) -> None:
    """Remove every stale generated ``mcp_*.py`` stub from the Tools tree."""

    tools_directory = volume_path(volume, "tools")
    try:
        listing = await filesystem.list(tools_directory)
    except _FilesystemError:
        return
    for entry in listing:
        if not entry.name.startswith(MCP_STUB_PREFIX) or not entry.name.endswith(
            ".py",
        ):
            continue
        path = volume_path(volume, "tools", entry.name)
        if path not in keep:
            await filesystem.remove(path)


def _emit(
    events: EventSink,
    kind: str,
    message: str,
    detail: Mapping[str, object] | None = None,
) -> None:
    emit_event(
        events,
        RuntimeDiagnostic(kind=kind, message=message, detail=detail or {}),
    )
