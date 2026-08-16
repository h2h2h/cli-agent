"""MCP server configuration discovery from logical capability source.

The control plane validates ``_mcp`` descriptions into provider-neutral
config facts without connecting to servers, materializing bindings, or
generating stubs; deployment owns those steps.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    parse_server_config,
)
from cli_agent.runtime._capability.source import _MCP_DIRECTORY
from cli_agent.runtime._capability.source_view import CapabilitySource
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.host import NULL_EVENTS, EventSink, emit_event


async def discover_configs(
    capability_view: CapabilitySource,
    events: EventSink = NULL_EVENTS,
) -> tuple[MCPServerConfig, ...]:
    """Read and validate every ``_mcp/<server>/config.json``.

    Servers are read from the logical view so Repertoire descriptions and
    real Workspace overrides both project. A whiteouted server is disabled
    without a diagnostic; a missing or structurally invalid config is
    reported through ``events`` and produces no config.
    """

    try:
        listing = await capability_view.list(_MCP_DIRECTORY)
    except _FilesystemError:
        return ()
    configs: list[MCPServerConfig] = []
    for entry in sorted(listing, key=lambda entry: entry.name):
        if entry.metadata.kind != "directory":
            continue
        server_name = entry.name
        relative = f"{_MCP_DIRECTORY}/{server_name}/config.json"
        try:
            inspection = await capability_view.inspect(relative)
        except ValueError:
            continue
        if inspection.provenance == "whiteout":
            continue
        try:
            content = await capability_view.read(relative)
        except _FilesystemError:
            _emit(
                events,
                "mcp.config_missing",
                f"MCP server {server_name} has no config.json",
                {"server": server_name},
            )
            continue
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _emit(
                events,
                "mcp.config_invalid",
                f"MCP server {server_name} config is invalid",
                {"server": server_name, "errors": (f"not readable JSON: {exc}",)},
            )
            continue
        config, errors = parse_server_config(raw, directory_name=server_name)
        if config is None:
            _emit(
                events,
                "mcp.config_invalid",
                f"MCP server {server_name} config is invalid",
                {"server": server_name, "errors": errors},
            )
            continue
        configs.append(config)
    return tuple(configs)


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
