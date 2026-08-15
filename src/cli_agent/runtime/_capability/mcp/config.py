"""MCP server configuration discovery from logical capability source.

The control plane validates ``_mcp`` descriptions into provider-neutral
config facts without connecting to servers, materializing bindings, or
generating stubs; deployment owns those steps.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    parse_server_config,
)
from cli_agent.runtime._capability.source import _MCP_DIRECTORY
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime.diagnostic import RuntimeDiagnostic


async def discover_configs(
    capability_view: _LogicalCapabilityView,
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
) -> tuple[MCPServerConfig, ...]:
    """Read and validate every ``_mcp/<server>/config.json``.

    Servers are read from the logical view so Repertoire descriptions and
    real Workspace overrides both project. A whiteouted server is disabled
    without a diagnostic; a missing or structurally invalid config is
    reported through ``on_diagnostic`` and produces no config.
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
                on_diagnostic,
                "mcp.config_missing",
                f"MCP server {server_name} has no config.json",
                {"server": server_name},
            )
            continue
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _emit(
                on_diagnostic,
                "mcp.config_invalid",
                f"MCP server {server_name} config is invalid",
                {"server": server_name, "errors": (f"not readable JSON: {exc}",)},
            )
            continue
        config, errors = parse_server_config(raw, directory_name=server_name)
        if config is None:
            _emit(
                on_diagnostic,
                "mcp.config_invalid",
                f"MCP server {server_name} config is invalid",
                {"server": server_name, "errors": errors},
            )
            continue
        configs.append(config)
    return tuple(configs)


def _emit(
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
    kind: str,
    message: str,
    detail: Mapping[str, object] | None = None,
) -> None:
    if on_diagnostic is None:
        return
    on_diagnostic(RuntimeDiagnostic(kind=kind, message=message, detail=detail or {}))
