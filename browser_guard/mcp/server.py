from ..dependencies.mcp import FastMCP
from ..web_navigator.interface import WebNavigatorBackend
from ..web_navigator.selenium_chrome import SeleniumChromeBackend
from .validator import ValidationError, validate_url

mcp = FastMCP("browser-guard")
backend: WebNavigatorBackend = SeleniumChromeBackend()


@mcp.tool()
def list_pages() -> list[dict]:
    """List all open browser tabs."""
    return [p.__dict__ for p in backend.list_pages()]


@mcp.tool()
def new_page(url: str | None = None) -> dict:
    """Open a new tab. Optional url is validated (paths OK, no query strings)."""
    if url:
        url = validate_url(url)
    return backend.new_page(url).__dict__


@mcp.tool()
def close_page(page_id: str) -> dict:
    """Close a tab by id."""
    backend.close_page(page_id)
    return {"closed": page_id}


@mcp.tool()
def select_page(page_id: str) -> dict:
    """Switch the active tab."""
    backend.select_page(page_id)
    return {"selected": page_id}


@mcp.tool()
def navigate(url: str) -> dict:
    """Navigate the current tab. Paths allowed; query strings/fragments rejected."""
    url = validate_url(url)
    return backend.navigate(url).__dict__


if __name__ == "__main__":
    mcp.run()
