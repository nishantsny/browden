from ..dependencies.mcp import FastMCP
from ..web_navigator.selenium_chrome import SeleniumChromeBackend
from ..web_navigator.session import PageSession
from .validator import ValidationError, validate_url

mcp = FastMCP("browser-guard")

_session: PageSession | None = None


def _get_session() -> PageSession:
    """Lazily build the coordinator (and start its reaper task).

    Called from inside a tool coroutine, so an event loop is already running —
    safe for ``PageSession.__init__`` to ``asyncio.create_task`` the reaper.
    Never invoked at import time.
    """
    global _session
    if _session is None:
        _session = PageSession(SeleniumChromeBackend())
    return _session


# -- navigation tools -------------------------------------------------------

@mcp.tool()
async def list_pages() -> list[dict]:
    """List all open browser tabs."""
    return [p.__dict__ for p in await _get_session().list_pages()]


@mcp.tool()
async def new_page(url: str | None = None) -> dict:
    """Open a new tab. Optional url is validated (paths OK, no query strings)."""
    if url:
        url = validate_url(url)
    return (await _get_session().new_page(url)).__dict__


@mcp.tool()
async def close_page(page_id: str) -> dict:
    """Close a tab by id."""
    await _get_session().close_page(page_id)
    return {"closed": page_id}


@mcp.tool()
async def select_page(page_id: str) -> dict:
    """Switch the active tab."""
    await _get_session().select_page(page_id)
    return {"selected": page_id}


@mcp.tool()
async def navigate(url: str, page_id: str) -> dict:
    """Navigate the named tab to url. Paths allowed; query strings/fragments rejected."""
    url = validate_url(url)
    result = await _get_session().navigate(url, page_id=page_id)
    return result if isinstance(result, dict) else result.__dict__


# -- DOM-query tools --------------------------------------------------------
#
# These take a REQUIRED page_id (the id from new_page / navigate / list_pages).
# Like navigate / select_page / force_reload_page, they never default to "the
# active tab": the active tab is shared state the human also controls, so an
# implicit default would silently act on whichever tab happens to be focused.
# A page_id that no longer names an open tab comes back as
# {"error": ..., "page_id": ...}.

@mcp.tool()
async def get_element_by_id(element_id: str, page_id: str,
                            include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.getElementById on a tab — one element node, or found=false (not an error) if absent."""
    return await _get_session().get_element_by_id(
        element_id, page_id=page_id, include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
async def get_elements_by_class_name(class_names: str, page_id: str,
                                     limit: int = 10, offset: int = 0,
                                     include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.getElementsByClassName on a tab — space-separated names, element must have ALL. Paginated."""
    return await _get_session().get_elements_by_class_name(
        class_names, page_id=page_id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
async def query_selector(css_selector: str, page_id: str,
                         include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelector on a tab — one element node, or found=false if no match. Invalid CSS → error."""
    return await _get_session().query_selector(
        css_selector, page_id=page_id, include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
async def query_selector_all(css_selector: str, page_id: str,
                             limit: int = 10, offset: int = 0,
                             include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelectorAll on a tab — paginated list of element nodes. Invalid CSS → error."""
    return await _get_session().query_selector_all(
        css_selector, page_id=page_id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
async def force_reload_page(page_id: str) -> dict:
    """Reload the named tab and refresh its cached DOM."""
    return await _get_session().force_reload_page(page_id=page_id)


if __name__ == "__main__":
    mcp.run()
