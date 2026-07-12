class ValidationError(ValueError):
    """Raised when an MCP tool input fails validation."""


def tab_gone_envelope(id: str) -> dict:
    """The standard error envelope returned when ``id`` no longer names an open tab.

    A tool that finds its tab has closed returns this (rather than raising) so the
    agent gets a clean, actionable message with the id echoed back. The one place
    the wording lives — server tools and the session manager both return it.
    """
    return {"error": f"tab {id} is no longer open — call list_tabs for current tabs", "id": id}
