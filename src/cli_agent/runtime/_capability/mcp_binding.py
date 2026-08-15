"""Runtime-owned MCP invocation binding rendering.

The CapabilityDeployment plane owns placement; this module owns the
provider-neutral mechanical detail of rendering the Runtime-owned binding
module. The binding carries only connection details and environment names;
model-visible stubs never see credentials or transport streams, and the
binding never lives in the Backend public protocol.
"""

from __future__ import annotations

from cli_agent.runtime._capability.mcp.facts import MCPServerConfig

_BINDING_FILENAME = "mcp_binding.py"


def binding_filename() -> str:
    """Return the binding module filename inside the Tool Runtime root."""

    return _BINDING_FILENAME


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
