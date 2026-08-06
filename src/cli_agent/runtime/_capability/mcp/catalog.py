"""Runtime-open MCP projection reconciliation from Repertoire descriptions."""

from __future__ import annotations

import asyncio
import keyword
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from cli_agent.runtime._backend import _BoundCapabilityView
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    load_server_config,
)
from cli_agent.runtime._capability.source import _MCP_DIRECTORY
from cli_agent.runtime._capability.workspace import _atomic_write
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

_MCP_STUB_PREFIX = "mcp_"
_DISCOVERY_RETRIES = 3

_STUB_RESERVED_NAMES = frozenset(
    {
        "call",
        "_connect",
        "_call_mcp",
        "_async_call_mcp",
        "_format_result",
        "_SERVER_COMMAND",
        "_SERVER_ARGS",
        "_ENV_NAMES",
        "_MCP_URL",
        "_HEADERS",
    }
)

_TYPE_HINTS = {
    "string": "str",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "object": "dict",
    "array": "list",
}


class _MCPCatalog:
    """Reconcile MCP server descriptions into generated Tool stubs.

    Alignment is a full rebuild with no manifest: stale ``mcp_*.py`` stubs are
    removed, then one stub is generated per successfully discovered server.
    """

    def __init__(self, servers: tuple[str, ...]) -> None:
        self.servers = servers

    @classmethod
    async def reconcile(
        cls,
        capability_view: _BoundCapabilityView,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> _MCPCatalog:
        """Align `_mcp` descriptions with generated Tools by full rebuild.

        Args:
            capability_view (`_BoundCapabilityView`):
                The materialized Bound Capability View; Repertoire ``_mcp``
                descriptions are read from its lower layer and projections
                are written into the Workspace ``tools`` tree.
            on_diagnostic (`Callable[[RuntimeDiagnostic], None] | None`):
                Optional Host callback for non-blocking reconcile notices.

        Returns:
            A catalog naming the servers successfully projected this run.
        """

        tools_directory = Path(capability_view.root) / "tools"
        _remove_stale_stubs(tools_directory)

        configs = await _valid_configs(capability_view, on_diagnostic)
        results = await asyncio.gather(
            *(_discover(config, on_diagnostic) for config in configs)
        )

        produced: list[str] = []
        for config, tools in zip(configs, results):
            if tools is None:
                continue
            stub_path = tools_directory / f"{_MCP_STUB_PREFIX}{config.name}.py"
            _atomic_write(stub_path, _render_stub(config, tools).encode("utf-8"))
            produced.append(config.name)
        return cls(tuple(sorted(produced)))


def _remove_stale_stubs(tools_directory: Path) -> None:
    """Remove every ``mcp_*.py`` stub from the Workspace Tools tree.

    The ``mcp_`` filename prefix is the sole ownership basis for MCP-generated
    artifacts; files without the prefix are never touched.
    """

    if not tools_directory.is_dir():
        return
    for path in tuple(tools_directory.iterdir()):
        if path.name.startswith(_MCP_STUB_PREFIX) and path.name.endswith(".py"):
            path.unlink(missing_ok=True)


async def _valid_configs(
    capability_view: _BoundCapabilityView,
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
) -> tuple[MCPServerConfig, ...]:
    """Read and validate every ``_mcp/<server>/config.json``, skipping bad ones.

    Servers are read from the Workspace ``_mcp`` view so Repertoire descriptions
    (mounted as exact lower links) and real Workspace overrides both project. A
    whiteouted server is disabled without a diagnostic; a missing or
    structurally invalid config is reported through ``on_diagnostic`` and
    produces no projection.
    """

    mcp_directory = Path(capability_view.root) / _MCP_DIRECTORY
    servers = (
        sorted(
            (path for path in mcp_directory.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
        if mcp_directory.is_dir()
        else ()
    )
    configs: list[MCPServerConfig] = []
    for directory in servers:
        server_name = directory.name
        config_path = directory / "config.json"
        try:
            inspection = await capability_view.inspect(
                (Path(_MCP_DIRECTORY) / server_name / "config.json").as_posix()
            )
        except ValueError:
            continue
        if inspection.provenance == "whiteout":
            continue
        if not config_path.is_file():
            _emit(
                on_diagnostic,
                "mcp.config_missing",
                f"MCP server {server_name} has no config.json",
                {"server": server_name},
            )
            continue
        config, errors = load_server_config(config_path)
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


async def _discover(
    config: MCPServerConfig,
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
) -> list[dict[str, Any]] | None:
    """Contact one server and return its discovered Tool metadata, or None.

    A failed initial attempt is retried up to ``_DISCOVERY_RETRIES`` times;
    exhaustion emits a diagnostic and returns None without a partial stub.
    """

    last_error: Exception | None = None
    for _ in range(_DISCOVERY_RETRIES):
        try:
            return await _list_tools_once(config)
        except Exception as exc:
            last_error = exc
    _emit(
        on_diagnostic,
        "mcp.discovery_failed",
        f"MCP server {config.name} discovery failed after "
        f"{_DISCOVERY_RETRIES} attempts",
        {"server": config.name, "error": str(last_error)},
    )
    return None


async def _list_tools_once(config: MCPServerConfig) -> list[dict[str, Any]]:
    from mcp import ClientSession

    async with _connect(config) as (read, write):
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
async def _connect(config: MCPServerConfig) -> AsyncIterator[tuple[Any, Any]]:
    if config.transport == "stdio":
        from mcp import StdioServerParameters, stdio_client

        command = config.command or ()
        params = StdioServerParameters(
            command=command[0],
            args=list(command[1:]),
            env=_resolved_env(config),
        )
        async with stdio_client(params) as streams:
            yield streams
        return

    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(
        headers=_resolved_headers(config) or None
    ) as http_client:
        async with streamable_http_client(
            config.url or "",
            http_client=http_client,
        ) as (read, write):
            yield (read, write)


def _resolved_env(config: MCPServerConfig) -> dict[str, str] | None:
    resolved = {name: os.environ[name] for name in config.env if name in os.environ}
    return resolved or None


def _resolved_headers(config: MCPServerConfig) -> dict[str, str]:
    return {
        header: os.environ[env_name]
        for header, env_name in config.headers
        if env_name in os.environ
    }


def _render_stub(
    config: MCPServerConfig,
    tools: list[dict[str, Any]],
) -> str:
    header = (
        _stub_docstring(config, tools)
        + "\n\nPARALLEL_SAFE = False\n"
        + "\nimport asyncio\nimport json\nimport os\n"
        + "from contextlib import asynccontextmanager\n\n"
        + "from mcp import ClientSession\n"
    )
    connect = (
        _stdio_connect(config) if config.transport == "stdio" else _http_connect(config)
    )
    used: set[str] = set(_STUB_RESERVED_NAMES)
    functions = [
        _tool_function(tool, func_name=_function_name(tool["name"], used))
        for tool in tools
    ]
    body = "\n\n".join([connect, *functions, _call_runtime()])
    return header + "\n\n" + body + "\n"


def _stub_docstring(
    config: MCPServerConfig,
    tools: list[dict[str, Any]],
) -> str:
    lines = [
        '"""',
        f"MCP Server ({config.transport}): {config.name}",
        "Generated by cli-agent from the Repertoire _mcp configuration.",
        "",
        "Usage:",
        f'  tools run "tools.mcp_{config.name}.<function>(...)"',
        "",
        "Available tools:",
    ]
    if tools:
        lines.extend(
            f"  - {tool['name']}: {_single_line(tool.get('description', ''))}"
            for tool in tools
        )
    else:
        lines.append("  (no tools discovered)")
    lines.append('"""')
    return "\n".join(lines)


def _stdio_connect(config: MCPServerConfig) -> str:
    command = config.command or ()
    return (
        "from mcp import StdioServerParameters\n"
        "from mcp.client.stdio import stdio_client\n"
        "\n"
        "_SERVER_COMMAND = " + repr(command[0]) + "\n"
        "_SERVER_ARGS = " + repr(list(command[1:])) + "\n"
        "_ENV_NAMES = " + repr(list(config.env)) + "\n"
        "\n"
        "\n"
        "@asynccontextmanager\n"
        "async def _connect():\n"
        '    """Create an STDIO connection to the MCP server."""\n'
        "    env = {name: os.environ[name] for name in _ENV_NAMES "
        "if name in os.environ}\n"
        "    params = StdioServerParameters(\n"
        "        command=_SERVER_COMMAND, args=_SERVER_ARGS, env=env or None\n"
        "    )\n"
        "    async with stdio_client(params) as streams:\n"
        "        yield streams\n"
    )


def _http_connect(config: MCPServerConfig) -> str:
    return (
        "import httpx2\n"
        "from mcp.client.streamable_http import streamable_http_client\n"
        "\n"
        "_MCP_URL = " + repr(config.url or "") + "\n"
        "_HEADERS = " + repr(list(config.headers)) + "\n"
        "\n"
        "\n"
        "@asynccontextmanager\n"
        "async def _connect():\n"
        '    """Create a streamable HTTP connection to the MCP server."""\n'
        "    headers = {name: os.environ[key] for name, key in _HEADERS "
        "if key in os.environ}\n"
        "    async with httpx2.AsyncClient(headers=headers or None) as http_client:\n"
        "        async with streamable_http_client(\n"
        "            _MCP_URL, http_client=http_client\n"
        "        ) as (read, write):\n"
        "            yield (read, write)\n"
    )


def _tool_function(tool: dict[str, Any], *, func_name: str) -> str:
    name = tool["name"]
    description = _single_line(tool.get("description", ""))
    schema = tool.get("input_schema") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    params = []
    for prop_name, prop_info in properties.items():
        type_hint = _type_hint(prop_info.get("type"))
        default = repr(prop_info.get("default"))
        if prop_name in required:
            params.append(f"{prop_name}: {type_hint}" if type_hint else prop_name)
        elif type_hint:
            params.append(f"{prop_name}: {type_hint} = {default}")
        else:
            params.append(f"{prop_name}={default}")
    signature = ", ".join(params)
    return (
        "def " + func_name + "(" + signature + "):\n"
        '    """' + description.replace('"""', '\\"\\"\\"') + '"""\n'
        "    return _call_mcp("
        + repr(name)
        + ", {k: v for k, v in locals().items() if v is not None})\n"
    )


def _call_runtime() -> str:
    return (
        "def call(tool_name, **kwargs):\n"
        '    """Call any MCP tool by name."""\n'
        "    return _call_mcp(tool_name, "
        "{k: v for k, v in kwargs.items() if v is not None})\n"
        "\n"
        "\n"
        "def _call_mcp(tool_name, arguments):\n"
        '    """Invoke one MCP tool over a fresh connection."""\n'
        "    return asyncio.run(_async_call_mcp(tool_name, arguments))\n"
        "\n"
        "\n"
        "async def _async_call_mcp(tool_name, arguments):\n"
        "    async with _connect() as (read, write):\n"
        "        async with ClientSession(read, write) as session:\n"
        "            await session.initialize()\n"
        "            result = await session.call_tool(tool_name, arguments)\n"
        "            return _format_result(result)\n"
        "\n"
        "\n"
        "def _format_result(result):\n"
        "    if result.is_error:\n"
        "        texts = [item.text for item in result.content "
        "if hasattr(item, 'text')]\n"
        "        raise RuntimeError(texts[0] if texts else "
        "'MCP tool returned an error')\n"
        "    if result.structured_content is not None:\n"
        "        return json.dumps(result.structured_content, ensure_ascii=False)\n"
        "    parts = []\n"
        "    for item in result.content:\n"
        "        if hasattr(item, 'text'):\n"
        "            parts.append(item.text)\n"
        "        elif hasattr(item, 'data'):\n"
        "            parts.append(str(item.data))\n"
        "    return '\\n'.join(parts) if parts else None\n"
    )


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _type_hint(schema_type: object) -> str:
    return _TYPE_HINTS.get(schema_type, "")


def _function_name(raw: str, used: set[str]) -> str:
    candidate = re.sub(r"\W", "_", raw)
    if not candidate or candidate[0].isdigit():
        candidate = "_" + candidate
    if keyword.iskeyword(candidate):
        candidate = candidate + "_"
    base = candidate
    counter = 2
    while candidate in used:
        candidate = f"{base}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _emit(
    on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
    kind: str,
    message: str,
    detail: Mapping[str, object] | None = None,
) -> None:
    if on_diagnostic is None:
        return
    on_diagnostic(RuntimeDiagnostic(kind=kind, message=message, detail=detail or {}))
