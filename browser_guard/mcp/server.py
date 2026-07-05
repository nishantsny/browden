import argparse
import atexit
import asyncio
import functools
import hashlib
import os
from pathlib import Path

from ..common.logger import logger
from ..configs.loader import (
    ConfigError,
    SAMPLE_ALLOWLIST,
    load_allowlist,
    resolve_allowlist_path,
)
from ..dependencies.mcp import FastMCP, Image
from ..web_navigator.tab_id import split_page_id
from ..web_navigator.selenium_chrome import SeleniumChromeBackend
from ..web_navigator.selenium_chrome import backend as selenium_backend
from ..web_navigator.session import BrowserSessionManager
from urllib.parse import urlparse

from .validator import (
    ActionAllowlist,
    ValidationError,
    is_add_to_cart,
    label_matches,
    validate_url,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_INSTRUCTIONS = (
    "browser-guard drives a real Chrome session. Within a single profile there "
    "is ONE browser session with one focused window, and the server does NOT "
    "serialize concurrent requests. Issue tool calls one at a time and wait for "
    "each to return before making the next. Firing calls in parallel — even "
    "against different tabs (page_ids) — races over the shared focused window "
    "and gives undefined results. (Distinct profiles are independent Chrome "
    "sessions and may run concurrently.)\n\n"
    "Each tab_id is globally unique and encodes its profile; pass it back verbatim. "
    "Profile is chosen only at new_blank_tab."
)

mcp = FastMCP(
    "browser-guard",
    instructions=_INSTRUCTIONS,
    host=os.environ.get("MCP_HOST", DEFAULT_HOST),
    port=int(os.environ.get("MCP_PORT", DEFAULT_PORT))
)

# Import-time default: the repo sample (reads open, writes deny-all), so unit
# tests and library imports see a deterministic policy. main() re-resolves
# (CLI > env > user config > sample) and replaces this before serving.
_ALLOWLIST = load_allowlist(SAMPLE_ALLOWLIST) if SAMPLE_ALLOWLIST.exists() else ActionAllowlist({})
logger.info("Browser Guard MCP module initialized")

# One BrowserSessionManager (hence one Chrome process) per profile directory. Requests
# that share a profile share its session and run serially through it; requests
# on *different* profiles get independent Chrome sessions and run concurrently,
# since distinct --user-data-dir profiles don't share window focus or the
# per-dir SingletonLock. The default (profile_dir=None) is resolved to the
# shared default profile *path* here (via _profile_key) before the backend is
# built, so existing single-profile behaviour is unchanged.
#
# Sessions are keyed by the profile's id namespace (the digest that prefixes
# every tab_id the profile mints), so _route can map an incoming tab_id back
# to its session. _digests memoizes profile path -> namespace.
_sessions: dict[str, BrowserSessionManager] = {}
_digests: dict[str, str] = {}
_atexit_registered = False


def _profile_key(profile_dir: str | None) -> str:
    """Canonical registry key for a profile dir; None -> the default profile's path.

    Reads the backend module's ``PROFILE_DIR`` for the default rather than
    constructing a backend, so it stays cheap and side-effect-free (and works
    when the backend class is mocked in tests).
    """
    if profile_dir:
        return str(Path(profile_dir).expanduser().resolve())
    return str(selenium_backend.PROFILE_DIR)


def _digest_for(key: str) -> str:
    """Mint (and memoize) the id namespace for a profile path.

    Normally the first 8 hex chars of the path's sha256; on the astronomically
    rare prefix collision with an already-registered profile, the digest is
    extended until unique, so a collision costs a longer id — never an error.
    """
    digest = _digests.get(key)
    if digest is None:
        full = hashlib.sha256(key.encode()).hexdigest()
        n = 8
        while full[:n] in _sessions:  # prefix taken by a colliding profile
            if n == len(full):
                raise RuntimeError(f"cannot mint a unique id namespace for {key}")
            n += 1
        digest = _digests[key] = full[:n]
    return digest


def _get_session(profile_dir: str | None = None) -> BrowserSessionManager:
    """Lazily build (and cache) the coordinator for ``profile_dir``.

    Called from inside a tool coroutine, so an event loop is already running —
    safe for ``BrowserSessionManager.__init__`` to ``asyncio.create_task`` the reaper.
    Never invoked at import time. Runs synchronously on the single event loop
    (no await between lookup and insert), so get-then-set cannot interleave.
    """
    global _atexit_registered
    key = _profile_key(profile_dir)
    digest = _digest_for(key)
    session = _sessions.get(digest)
    if session is None:
        logger.info(f"Initializing BrowserSessionManager (profile={key})")
        session = BrowserSessionManager(SeleniumChromeBackend(
            profile_dir=key, id_namespace=digest))
        _sessions[digest] = session
        if not _atexit_registered:
            atexit.register(lambda: logger.info("MCP Server shutting down"))
            _atexit_registered = True
    return session


class UnknownPageIdError(LookupError):
    """A tab_id whose namespace matches no active session."""

    def __init__(self, tab_id: str):
        super().__init__(f"unknown tab_id {tab_id!r}")
        self.envelope = {
            "error": "unknown tab_id — its profile has no active session; call new_blank_tab to start one (or list_tabs)",
            "tab_id": tab_id,
        }


def _route(tab_id: str) -> BrowserSessionManager:
    namespace, _handle = split_page_id(tab_id)
    session = _sessions.get(namespace)
    if session is None:
        raise UnknownPageIdError(tab_id)
    return session


def _tool(fn):
    """Turn UnknownPageIdError into the standard error envelope tools return."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except UnknownPageIdError as e:
            return e.envelope
    return wrapper


# -- navigation tools -------------------------------------------------------

@mcp.tool()
async def list_tabs() -> list[dict]:
    """List all open browser tabs across all profiles' sessions.

    Within a profile this drives the shared focused window like any other tab
    call: issue calls sequentially — concurrent requests (even to different
    page_ids) race over the focused window and give undefined results.

    Profiles whose Chrome has exited are skipped (they have no open tabs);
    listing never relaunches a browser.
    """
    logger.info("Tool called: list_tabs")

    async def _fetch(session: BrowserSessionManager) -> list[dict]:
        if not await session.is_live():
            logger.info(f"list_tabs: skipping dead session (profile={session.profile_dir})")
            return []
        return await session.list_tabs()

    listings = await asyncio.gather(*(_fetch(s) for s in _sessions.values()))
    logger.info("Tool finished: list_tabs")
    return [tab for tabs in listings for tab in tabs]


@mcp.tool()
async def new_blank_tab(profile_dir: str | None = None) -> dict:
    """Open a new blank tab.

    Optional profile_dir runs the request in an independent Chrome profile; the
    returned tab_id is only valid for that same profile.

    Only one tab can be driven at a time within a profile: interact with tabs
    sequentially — concurrent requests (even to different page_ids) race over
    the shared focused window and give undefined results.
    """
    logger.info(f"Tool called: new_blank_tab (profile_dir={profile_dir!r})")

    session = _get_session(profile_dir)
    res = await session.new_blank_tab()
    logger.info("Tool finished: new_blank_tab")
    return res.as_page_dict(profile_dir=session.profile_dir)


@mcp.tool()
@_tool
async def close_tab(tab_id: str) -> dict:
    """Close a tab by id."""
    logger.info(f"Tool called: close_tab (tab_id={tab_id!r})")
    session = _route(tab_id)
    await session.close_tab(tab_id)
    logger.info("Tool finished: close_tab")
    return {"closed": tab_id}


@mcp.tool()
@_tool
async def select_tab(tab_id: str) -> dict:
    """Switch the active tab."""
    logger.info(f"Tool called: select_tab (tab_id={tab_id!r})")
    session = _route(tab_id)
    await session.select_tab(tab_id)
    logger.info("Tool finished: select_tab")
    return {"selected": tab_id}


@mcp.tool()
@_tool
async def navigate(url: str, tab_id: str) -> dict:
    """Navigate the named tab to url. Url is gated by the per-host allowlist (query strings and fragments pass through)."""
    logger.info(f"Tool called: navigate (url={url!r}, tab_id={tab_id!r})")
    session = _route(tab_id)
    url = validate_url(url, _ALLOWLIST.section("read"))
    result = await session.navigate(url, tab_id=tab_id)
    logger.info("Tool finished: navigate")
    # The session returns the tab-gone envelope as a dict, a TabInfo otherwise.
    return result if isinstance(result, dict) else result.as_page_dict()


# -- write tools ------------------------------------------------------------

@mcp.tool()
@_tool
async def add_to_cart(css_selector: str, tab_id: str) -> dict:
    """Click an "Add to cart" control on a tab — the only write action.

    Two server-side gates, both default-deny, must pass:
      1. The tab's host must be listed under the ``add_to_cart`` section of the
         allowlist. The shipped default has no hosts enabled — the amazon.com
         entry in allowlist.yaml is commented out until you opt in.
      2. ``css_selector`` must resolve to exactly one element that is, by
         trustworthy signals, a genuine add-to-cart button — not Buy Now,
         checkout, subscribe, remove, or an agent-targeted decoy.
    Either gate failing raises a ValidationError and nothing is clicked.
    """
    logger.info(f"Tool called: add_to_cart (css_selector={css_selector!r}, tab_id={tab_id!r})")
    session = _route(tab_id)

    # Gate 1: per-action host allowlist, checked against the tab's live URL.
    url = await session.current_url(tab_id=tab_id)
    if url is None:
        return {"error": f"tab {tab_id} is no longer open — call list_tabs for current tabs",
                "tab_id": tab_id}
    validate_url(url, _ALLOWLIST.section("add_to_cart"))  # raises if host not allowed

    # Gate 2: the element must be a single, genuine add-to-cart control.
    found = await session.query_selector_all(css_selector, tab_id=tab_id, limit=2)
    if "error" in found:
        return found
    total = found["total_count"]
    if total == 0:
        raise ValidationError(f"no element matches selector {css_selector!r}")
    if total > 1:
        raise ValidationError(f"selector {css_selector!r} is ambiguous ({total} matches) — refusing to click")
    node = found["elements"][0]
    if not is_add_to_cart(node):
        raise ValidationError(
            "selected element is not a recognized add-to-cart control — refusing to click")

    # Gate 3: the site-specific required button text from the allowlist (e.g.
    # amazon.com must display "Add to cart").
    host = urlparse(url).hostname or ""
    label_re = _ALLOWLIST.label_pattern("add_to_cart", host)
    if label_re is not None and not label_matches(node, label_re):
        raise ValidationError(
            f"control text does not match the required add-to-cart label for {host} — refusing to click")

    result = await session.add_to_cart_click(css_selector, tab_id=tab_id)
    logger.info("Tool finished: add_to_cart")
    return result


# -- DOM-query tools --------------------------------------------------------
#
# These take a REQUIRED tab_id (the id from new_blank_tab / navigate / list_tabs).
# Like navigate / select_tab / force_reload_tab, they never default to "the
# active tab": the active tab is shared state the human also controls, so an
# implicit default would silently act on whichever tab happens to be focused.
# A tab_id that no longer names an open tab comes back as
# {"error": ..., "tab_id": ...}.

@mcp.tool()
@_tool
async def get_element_by_id(element_id: str, tab_id: str,
                            include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.getElementById on a tab — one element node, or found=false (not an error) if absent."""
    logger.info(f"Tool called: get_element_by_id (element_id={element_id!r}, tab_id={tab_id!r})")
    session = _route(tab_id)
    result = await session.get_element_by_id(
        element_id, tab_id=tab_id, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_element_by_id")
    return result


@mcp.tool()
@_tool
async def get_elements_by_class_name(class_names: str, tab_id: str,
                                     limit: int = 10, offset: int = 0,
                                     include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.getElementsByClassName on a tab — space-separated names, element must have ALL. Paginated."""
    logger.info(f"Tool called: get_elements_by_class_name (class_names={class_names!r}, tab_id={tab_id!r})")
    session = _route(tab_id)
    result = await session.get_elements_by_class_name(
        class_names, tab_id=tab_id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_elements_by_class_name")
    return result


@mcp.tool()
@_tool
async def query_selector(css_selector: str, tab_id: str,
                         include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelector on a tab — one element node, or found=false if no match. Invalid CSS → error."""
    logger.info(f"Tool called: query_selector (css_selector={css_selector!r}, tab_id={tab_id!r})")
    session = _route(tab_id)
    result = await session.query_selector(
        css_selector, tab_id=tab_id, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: query_selector")
    return result


@mcp.tool()
@_tool
async def query_selector_all(css_selector: str, tab_id: str,
                             limit: int = 10, offset: int = 0,
                             include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelectorAll on a tab — paginated list of element nodes. Invalid CSS → error."""
    logger.info(f"Tool called: query_selector_all (css_selector={css_selector!r}, tab_id={tab_id!r})")
    session = _route(tab_id)
    result = await session.query_selector_all(
        css_selector, tab_id=tab_id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: query_selector_all")
    return result


@mcp.tool()
@_tool
async def screenshot(tab_id: str):
    """Capture a PNG screenshot of a tab's current viewport.

    Read-only: it grabs live pixels from the rendered tab and never mutates it
    or the DOM cache. Returns the image on success, or
    ``{"error": ..., "tab_id": ...}`` if the tab is no longer open.
    """
    logger.info(f"Tool called: screenshot (tab_id={tab_id!r})")
    session = _route(tab_id)
    result = await session.screenshot(tab_id=tab_id)
    if isinstance(result, dict):  # tab gone — structured error, not an image
        return result
    logger.info("Tool finished: screenshot")
    return Image(data=result, format="png")


@mcp.tool()
@_tool
async def force_reload_tab(tab_id: str) -> dict:
    """Reload the named tab and refresh its cached DOM."""
    logger.info(f"Tool called: force_reload_tab (tab_id={tab_id!r})")
    session = _route(tab_id)
    result = await session.force_reload_tab(tab_id=tab_id)
    logger.info("Tool finished: force_reload_tab")
    return result


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: resolve + load the allowlist, then serve.

    The loaded, schema-verified allowlist replaces the module default so every
    tool (the internal consumers of ``_ALLOWLIST``) gates against the config
    the operator chose.
    """
    global _ALLOWLIST
    parser = argparse.ArgumentParser(
        prog="browser-guard", description="Browser Guard MCP server")
    parser.add_argument(
        "--allowlist",
        help="Path to the allowlist YAML config (default: $BROWSER_GUARD_ALLOWLIST, "
             "then ~/.browser_guard/allowlist.yaml, then the repo sample)")
    args = parser.parse_args(argv)

    path = resolve_allowlist_path(args.allowlist)
    if path is None:
        parser.error("no allowlist config found — run setup/onetime_setup.py or pass --allowlist")
    try:
        _ALLOWLIST = load_allowlist(path)
    except ConfigError as e:
        parser.error(str(e))
    logger.info(f"Loaded allowlist config from {path}")

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    logger.info(f"MCP Server starting (transport={transport})")
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
