"""Local Workspace MCP discovery and invocation binding rendering.

The CapabilityDeployment plane owns placement; this module owns the Local
mechanical detail of contacting servers and rendering the Runtime-owned
binding module. Discovery connects to each configured server from the
Local execution base environment (never raw ``os.environ``) and returns
provider-neutral ``_MCPServerFacts``. The rendered binding carries only
connection details and environment names; model-visible stubs never see
credentials or transport streams.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    _MCPServerFacts,
    _MCPToolFacts,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

_DISCOVERY_RETRIES = 3
_BINDING_FILENAME = "mcp_binding.py"


async def discover_servers(
    configs: tuple[MCPServerConfig, ...],
    base_environment: Mapping[str, str],
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
) -> tuple[_MCPServerFacts, ...]:
    """Discover every configured server and return provider-neutral facts.

    Servers are contacted in parallel; each failed attempt is retried up to
    ``_DISCOVERY_RETRIES`` times, and exhaustion emits a diagnostic and
    produces no facts for that server.
    """

    results = await asyncio.gather(
        *(_discover(config, base_environment, on_diagnostic) for config in configs)
    )
    return tuple(fact for fact in results if fact is not None)


def binding_filename() -> str:
    """Return the binding module filename inside the Tool Runtime root."""

    return _BINDING_FILENAME


async def _discover(
    config: MCPServerConfig,
    base_environment: Mapping[str, str],
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
) -> _MCPServerFacts | None:
    """Contact one server and return its discovered Tool facts, or None."""

    last_error: Exception | None = None
    for _ in range(_DISCOVERY_RETRIES):
        try:
            tools = await _list_tools_once(config, base_environment)
        except Exception as exc:
            last_error = exc
        else:
            return _MCPServerFacts(
                name=config.name,
                tools=tuple(
                    _MCPToolFacts(
                        name=tool["name"],
                        description=tool["description"],
                        input_schema=tool["input_schema"],
                    )
                    for tool in tools
                ),
            )
    _emit(
        on_diagnostic,
        "mcp.discovery_failed",
        f"MCP server {config.name} discovery failed after "
        f"{_DISCOVERY_RETRIES} attempts",
        {"server": config.name, "error": str(last_error)},
    )
    return None


async def _list_tools_once(
    config: MCPServerConfig,
    base_environment: Mapping[str, str],
) -> list[dict[str, Any]]:
    from mcp import ClientSession

    async with _connect(config, base_environment) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.input_schema or {},
                }
                for tool in result.tools
            ]


@asynccontextmanager
async def _connect(
    config: MCPServerConfig,
    base_environment: Mapping[str, str],
) -> Any:
    if config.transport == "stdio":
        from mcp import StdioServerParameters, stdio_client

        command = config.command or ()
        params = StdioServerParameters(
            command=command[0],
            args=list(command[1:]),
            env=_resolved_env(config, base_environment),
        )
        async with stdio_client(params) as streams:
            yield streams
        return

    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(
        headers=_resolved_headers(config, base_environment) or None
    ) as http_client:
        async with streamable_http_client(
            config.url or "",
            http_client=http_client,
        ) as (read, write):
            yield (read, write)


def _resolved_env(
    config: MCPServerConfig,
    base_environment: Mapping[str, str],
) -> dict[str, str] | None:
    resolved = {
        name: base_environment[name] for name in config.env if name in base_environment
    }
    return resolved or None


def _resolved_headers(
    config: MCPServerConfig,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    return {
        header: base_environment[env_name]
        for header, env_name in config.headers
        if env_name in base_environment
    }


def render_binding(configs: tuple[MCPServerConfig, ...]) -> str:
    """Render the Runtime-owned worker-side invocation binding module.

    The binding carries connection details and environment names for every
    successfully discovered server; it never contains resolved env values.
    Model-visible stubs only import ``call_tool`` from this module.
    """

    entries = "\n".join(
        f"    {config.name!r}: {_server_binding(config)}," for config in configs
    )
    return (
        '"""Runtime-owned MCP invocation binding.\n'
        "\n"
        "Materialized into the Tool Runtime by the CapabilityDeployment.\n"
        "Stubs only call call_tool; connection details and environment\n"
        "names stay in this Runtime-owned module, never in stubs.\n"
        '"""\n'
        "\n"
        "import asyncio\n"
        "import os\n"
        "from contextlib import asynccontextmanager\n"
        "\n"
        "from mcp import ClientSession\n"
        "\n"
        "_SERVERS = {\n"
        f"{entries}\n"
        "}\n"
        "\n"
        "\n"
        "@asynccontextmanager\n"
        "async def _connect(name):\n"
        '    """Create one fresh connection to a discovered server."""\n'
        "    server = _SERVERS[name]\n"
        '    if server["transport"] == "stdio":\n'
        "        from mcp import StdioServerParameters\n"
        "        from mcp.client.stdio import stdio_client\n"
        "\n"
        "        env = {name: os.environ[name] for name in server['env']\n"
        "               if name in os.environ} or None\n"
        "        params = StdioServerParameters(\n"
        "            command=server['command'],\n"
        "            args=list(server['args']),\n"
        "            env=env,\n"
        "        )\n"
        "        async with stdio_client(params) as streams:\n"
        "            yield streams\n"
        "        return\n"
        "\n"
        "    import httpx2\n"
        "    from mcp.client.streamable_http import streamable_http_client\n"
        "\n"
        "    headers = {header: os.environ[key] for header, key\n"
        "               in server['headers'] if key in os.environ}\n"
        "    async with httpx2.AsyncClient(headers=headers or None) as http_client:\n"
        "        async with streamable_http_client(\n"
        "            server['url'],\n"
        "            http_client=http_client,\n"
        "        ) as (read, write):\n"
        "            yield (read, write)\n"
        "\n"
        "\n"
        "async def _async_call_tool(name, tool_name, arguments):\n"
        '    """Invoke one MCP tool over a fresh connection."""\n'
        "    async with _connect(name) as (read, write):\n"
        "        async with ClientSession(read, write) as session:\n"
        "            await session.initialize()\n"
        "            return await session.call_tool(tool_name, arguments)\n"
        "\n"
        "\n"
        "def call_tool(name, tool_name, arguments):\n"
        '    """Invoke one MCP tool synchronously inside the worker."""\n'
        "    return asyncio.run(_async_call_tool(name, tool_name, arguments))"
    )


def _server_binding(config: MCPServerConfig) -> dict[str, object]:
    if config.transport == "stdio":
        command = list(config.command or ())
        return {
            "transport": "stdio",
            "command": command[0],
            "args": command[1:],
            "env": list(config.env),
        }
    return {
        "transport": "http",
        "url": config.url or "",
        "headers": list(config.headers),
    }


def _emit(
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
    kind: str,
    message: str,
    detail: Mapping[str, object] | None = None,
) -> None:
    if on_diagnostic is None:
        return
    on_diagnostic(RuntimeDiagnostic(kind=kind, message=message, detail=detail or {}))
