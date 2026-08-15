"""Runtime-open MCP projection reconciliation from Workspace descriptions.

Discovery is delegated to the Backend Workspace MCP Runtime: the Catalog
reads and validates the ``_mcp`` descriptions from the Bound Capability
View, asks the Runtime for provider-neutral server facts, and projects one
generated stub per discovered server into the Workspace Tools tree. Stubs
only call the Runtime-materialized ``mcp_binding`` module; the Catalog never
holds a transport stream, client, or subprocess, and never reads env values.
"""

from __future__ import annotations

import keyword
import posixpath
import re
from collections.abc import Callable, Mapping

from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _FileWriteRequest,
    _MCPToolFacts,
)
from cli_agent.runtime._capability.facts import _FilesystemError
from cli_agent.runtime._capability.mcp.config import discover_configs
from cli_agent.runtime._capability.mcp.facts import MCPServerConfig
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

_MCP_STUB_PREFIX = "mcp_"

_STUB_RESERVED_NAMES = frozenset(
    {
        "call",
        "_call_mcp",
        "_format_result",
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
        backend: _BackendWorkspace,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
        *,
        configs: tuple[MCPServerConfig, ...] | None = None,
    ) -> _MCPCatalog:
        """Align ``_mcp`` descriptions with generated Tools by full rebuild.

        Args:
            backend (`_BackendWorkspace`):
                The live Backend Workspace; its MCP Runtime performs
                discovery and materializes the invocation binding and its
                Filesystem receives the stub projection.
            on_diagnostic (`Callable[[RuntimeDiagnostic], None] | None`):
                Optional Host callback for non-blocking reconcile notices.
            configs (`tuple[MCPServerConfig, ...] | None`):
                Optional pre-discovered configs; when omitted they are
                discovered from the Bound Capability View.

        Returns:
            A catalog naming the servers successfully projected this run.
        """

        if configs is None:
            configs = await discover_configs(backend.capabilities, on_diagnostic)
        discovered = {
            fact.name: fact
            for fact in await backend.mcp.discover(configs, on_diagnostic)
        }
        await _remove_stale_stubs(backend)
        try:
            await backend.mcp.materialize_binding(
                tuple(config for config in configs if config.name in discovered)
            )
        except Exception as exc:
            _emit(
                on_diagnostic,
                "mcp.binding_failed",
                "MCP invocation binding could not be materialized",
                {"error": str(exc)},
            )
            return cls(())

        produced: list[str] = []
        for config in configs:
            fact = discovered.get(config.name)
            if fact is None:
                continue
            path = _stub_path(backend, config.name)
            await backend.filesystem.write(
                _FileWriteRequest(
                    path=path,
                    content=_render_stub(config, fact.tools).encode("utf-8"),
                )
            )
            produced.append(config.name)
        return cls(tuple(sorted(produced)))


def _stub_path(backend: _BackendWorkspace, server_name: str) -> str:
    return posixpath.join(
        backend.capabilities.root,
        "tools",
        f"{_MCP_STUB_PREFIX}{server_name}.py",
    )


async def _remove_stale_stubs(backend: _BackendWorkspace) -> None:
    """Remove every ``mcp_*.py`` stub from the Workspace Tools tree.

    The ``mcp_`` filename prefix is the sole ownership basis for MCP-generated
    artifacts; files without the prefix are never touched.
    """

    tools_directory = posixpath.join(backend.capabilities.root, "tools")
    try:
        listing = await backend.filesystem.list(tools_directory)
    except _FilesystemError:
        return
    for entry in listing:
        if entry.name.startswith(_MCP_STUB_PREFIX) and entry.name.endswith(".py"):
            await backend.filesystem.remove(posixpath.join(tools_directory, entry.name))


def _render_stub(
    config: MCPServerConfig,
    tools: tuple[_MCPToolFacts, ...],
) -> str:
    header = (
        _stub_docstring(config, tools)
        + "\n\nPARALLEL_SAFE = False\n"
        + "\nimport json\n\n"
        + "from mcp_binding import call_tool as _call_mcp\n"
    )
    used: set[str] = set(_STUB_RESERVED_NAMES)
    functions = [
        _tool_function(config.name, tool, func_name=_function_name(tool.name, used))
        for tool in tools
    ]
    body = "\n\n".join([*functions, _call_runtime(config.name)])
    return header + "\n\n" + body + "\n"


def _stub_docstring(
    config: MCPServerConfig,
    tools: tuple[_MCPToolFacts, ...],
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
            f"  - {tool.name}: {_single_line(tool.description)}" for tool in tools
        )
    else:
        lines.append("  (no tools discovered)")
    lines.append('"""')
    return "\n".join(lines)


def _tool_function(server_name: str, tool: _MCPToolFacts, *, func_name: str) -> str:
    schema = tool.input_schema or {}
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
        '    """' + _single_line(tool.description).replace('"""', '\\"\\"\\"') + '"""\n'
        "    return _format_result(_call_mcp("
        + repr(server_name)
        + ", "
        + repr(tool.name)
        + ", {k: v for k, v in locals().items() if v is not None}))\n"
    )


def _call_runtime(server_name: str) -> str:
    return (
        "def call(tool_name, **kwargs):\n"
        '    """Call any MCP tool by name."""\n'
        "    return _format_result(_call_mcp("
        + repr(server_name)
        + ", tool_name, {k: v for k, v in kwargs.items() if v is not None}))\n"
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
