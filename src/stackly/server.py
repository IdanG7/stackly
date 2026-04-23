"""FastMCP server wiring for Stackly.

Creates a single ``FastMCP`` instance, attaches a single ``DebugSession``, and
registers all tools. Callers choose the transport via :func:`run`.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from stackly import tools
from stackly.session import DebugSession


def build_app() -> tuple[FastMCP, DebugSession]:
    """Construct the server and its backing session. Separate from :func:`run`
    so tests can exercise the server without opening a network socket."""
    mcp = FastMCP("stackly")
    session = DebugSession()
    tools.register(mcp, session)
    return mcp, session


def run(
    transport: Literal["http", "stdio"] = "http",
    host: str = "127.0.0.1",
    port: int = 8585,
) -> None:
    """Start the Stackly MCP server on the chosen transport.

    HTTP transport exposes ``http://{host}:{port}/mcp`` (the MCP Streamable
    HTTP convention). Stdio transport is for MCP clients that launch the
    server as a subprocess (Claude Desktop, some Cursor configs).
    """
    mcp, _session = build_app()
    if transport == "http":
        # FastMCP 1.27's HTTP transport is named "streamable-http" internally
        # but accepts "http" as an alias. Settings host/port go through
        # instance attributes.
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
