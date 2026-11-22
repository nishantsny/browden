"""Anti-corruption wrapper around the PyPI `mcp` SDK.

All in-project code imports MCP framework symbols from here, never directly
from the SDK. This keeps the surface we depend on visible in one file and
makes it trivial to mock in unit tests.
"""
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

__all__ = ["MCPServer", "Image", "ToolError"]
