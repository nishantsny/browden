"""Anti-corruption wrapper around the PyPI `mcp` SDK.

All in-project code imports MCP framework symbols from here, never directly
from the SDK. This keeps the surface we depend on visible in one file and
makes it trivial to mock in unit tests.
"""
from mcp.server.fastmcp import FastMCP

__all__ = ["FastMCP"]
