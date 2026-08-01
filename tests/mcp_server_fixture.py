"""A minimal in-repo MCP stdio server used as an offline test fixture.

Run as a subprocess through the Workspace config ``command`` so discovery and
invocation exercises the real stdio transport without external services.
"""

from __future__ import annotations

import asyncio

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server(name="cli-agent-fixture", version="1.0.0")


async def handle_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="add",
                description="Add two numbers.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer", "description": "First number."},
                        "b": {"type": "integer", "description": "Second number."},
                    },
                    "required": ["a", "b"],
                },
            ),
            types.Tool(
                name="say",
                description="Echo text.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to echo."},
                    },
                    "required": ["text"],
                },
            ),
        ]
    )


async def handle_call_tool(ctx, params):
    if params.name == "add":
        total = params.arguments.get("a", 0) + params.arguments.get("b", 0)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(total))]
        )
    if params.name == "say":
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=params.arguments.get("text", ""),
                )
            ]
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="unknown tool")],
        is_error=True,
    )


server.add_request_handler(
    "tools/list", types.PaginatedRequestParams, handle_list_tools
)
server.add_request_handler(
    "tools/call", types.CallToolRequestParams, handle_call_tool
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
