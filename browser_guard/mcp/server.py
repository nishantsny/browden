import atexit
import os
from pathlib import Path

from ..common.logger import logger
from ..dependencies.mcp import FastMCP
from ..web_navigator.selenium_chrome import SeleniumChromeBackend
from ..web_navigator.session import PageSession
from .validator import Allowlist, ValidationError, validate_url

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

mcp = FastMCP(
    "browser-guard",
    host=os.environ.get("MCP_HOST", DEFAULT_HOST),
    port=int(os.environ.get("MCP_PORT", DEFAULT_PORT))
)

_ALLOWLIST = Allowlist.from_file(Path(__file__).parent / "validator" / "allowlist.json")
_session: PageSession | None = None


def _get_session() -> PageSession:
    """Lazily build the coordinator (and start its reaper task).

    Called from inside a tool coroutine, so an event loop is already running —
    safe for ``PageSession.__init__`` to ``asyncio.create_task`` the reaper.
    Never invoked at import time.
    """
    global _session
    if _session is None:
        logger.info("Initializing PageSession")
        _session = PageSession(SeleniumChromeBackend())
        atexit.register(lambda: logger.info("MCP Server shutting down"))
    return _session


# -- navigation tools -------------------------------------------------------

@mcp.tool()
async def list_pages() -> list[dict]:
    """List all open browser tabs."""
    logger.info("Tool called: list_pages")
    result = [p.__dict__ for p in await _get_session().list_pages()]
    logger.info("Tool finished: list_pages")
    return result


@mcp.tool()
async def new_page(url: str | None = None) -> dict:
    """Open a new tab. Optional url is gated by the per-host allowlist (query strings and fragments pass through)."""
    logger.info(f"Tool called: new_page (url={url!r})")
    if url:
        url = validate_url(url, _ALLOWLIST)
    result = (await _get_session().new_page(url)).__dict__
    logger.info("Tool finished: new_page")
    return result


@mcp.tool()
async def close_page(page_id: str) -> dict:
    """Close a tab by id."""
    logger.info(f"Tool called: close_page (page_id={page_id!r})")
    await _get_session().close_page(page_id)
    logger.info("Tool finished: close_page")
    return {"closed": page_id}


@mcp.tool()
async def select_page(page_id: str) -> dict:
    """Switch the active tab."""
    logger.info(f"Tool called: select_page (page_id={page_id!r})")
    await _get_session().select_page(page_id)
    logger.info("Tool finished: select_page")
    return {"selected": page_id}


@mcp.tool()
async def navigate(url: str, page_id: str) -> dict:
    """Navigate the named tab to url. Url is gated by the per-host allowlist (query strings and fragments pass through)."""
    logger.info(f"Tool called: navigate (url={url!r}, page_id={page_id!r})")
    url = validate_url(url, _ALLOWLIST)
    result = await _get_session().navigate(url, page_id=page_id)
    logger.info("Tool finished: navigate")
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
    logger.info(f"Tool called: get_element_by_id (element_id={element_id!r}, page_id={page_id!r})")
    result = await _get_session().get_element_by_id(
        element_id, page_id=page_id, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_element_by_id")
    return result


@mcp.tool()
async def get_elements_by_class_name(class_names: str, page_id: str,
                                     limit: int = 10, offset: int = 0,
                                     include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.getElementsByClassName on a tab — space-separated names, element must have ALL. Paginated."""
    logger.info(f"Tool called: get_elements_by_class_name (class_names={class_names!r}, page_id={page_id!r})")
    result = await _get_session().get_elements_by_class_name(
        class_names, page_id=page_id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_elements_by_class_name")
    return result


@mcp.tool()
async def query_selector(css_selector: str, page_id: str,
                         include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelector on a tab — one element node, or found=false if no match. Invalid CSS → error."""
    logger.info(f"Tool called: query_selector (css_selector={css_selector!r}, page_id={page_id!r})")
    result = await _get_session().query_selector(
        css_selector, page_id=page_id, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: query_selector")
    return result


@mcp.tool()
async def query_selector_all(css_selector: str, page_id: str,
                             limit: int = 10, offset: int = 0,
                             include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelectorAll on a tab — paginated list of element nodes. Invalid CSS → error."""
    logger.info(f"Tool called: query_selector_all (css_selector={css_selector!r}, page_id={page_id!r})")
    result = await _get_session().query_selector_all(
        css_selector, page_id=page_id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: query_selector_all")
    return result


@mcp.tool()
async def force_reload_page(page_id: str) -> dict:
    """Reload the named tab and refresh its cached DOM."""
    logger.info(f"Tool called: force_reload_page (page_id={page_id!r})")
    result = await _get_session().force_reload_page(page_id=page_id)
    logger.info("Tool finished: force_reload_page")
    return result


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    logger.info(f"MCP Server starting (transport={transport})")
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
