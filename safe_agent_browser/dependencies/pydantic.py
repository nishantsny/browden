"""Anti-corruption wrapper around `pydantic`.

All in-project code imports pydantic symbols from here, never directly from the
third-party package. ``Field`` is used to hang a ``description`` on a tool
parameter: FastMCP builds each tool's JSON schema from its annotations, so an
``Annotated[T, Field(description=...)]`` parameter is the only way a caps or
semantics note reaches an MCP client. See ``mcp/server.py``'s DOM-query tools.
"""
from pydantic import Field

__all__ = ["Field"]
