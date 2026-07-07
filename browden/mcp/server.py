import argparse
import asyncio
import functools
import os
from collections.abc import Awaitable, Callable
from pathlib import Path


from ..common.logger import logger
from ..configs.loader import (
    ConfigError,
    SAMPLE_ALLOWLIST,
    load_allowlist,
    resolve_allowlist_path,
)
from ..dependencies.mcp import FastMCP, Image
from ..web_navigator.selenium_chrome import SeleniumChromeBackend
from .session_management.BrowserSessionStore import BrowserSessionStore, UnknownTabError
from urllib.parse import urlparse

from .validator import (
    ActionAllowlist,
    ValidationError,
    field_label_matches,
    is_clickable_control,
    is_fillable_control,
    label_matches,
    validate_url,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_INSTRUCTIONS = (
    "browden drives a real Chrome session. Within a single profile-dir there is ONE browser session. You can open multiple tabs within that one session, though concurrent requests are only supported across different sessions, but within the same session (this is a limitation of selenium: the underlying automation library). A new profile-dir can be chosen while crating a new tab. If you choose a previously used profile-dir, then the previous session will be reused. Creating a new tab will return a tab-id which is unique across all sessions, pass it back verbatim on other tools."
)

mcp = FastMCP(
    "browden",
    instructions=_INSTRUCTIONS,
    host=os.environ.get("MCP_HOST", DEFAULT_HOST),
    port=int(os.environ.get("MCP_PORT", DEFAULT_PORT))
)

# Import-time default: the repo sample (reads open, writes deny-all), so unit
# tests and library imports see a deterministic policy. main() re-resolves
# (CLI > env > user config > sample) and replaces this before serving.
_ALLOWLIST = load_allowlist(SAMPLE_ALLOWLIST) if SAMPLE_ALLOWLIST.exists() else ActionAllowlist({})
logger.info("Browden MCP module initialized")

# All per-profile session state and the customer<->backend id mapping live in
# the store (see session_management/BrowserSessionStore.py).
_store = BrowserSessionStore()


def _default_profile_dir() -> Path:
    """The shared default Chrome profile path (honours ``XDG_CACHE_HOME``).

    Lives here, not in the backend: the backend never falls back to a default —
    the server is the caller that decides which profile, and hands the backend a
    concrete path.
    """
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "browden" / "chrome-profile"


def _resolve_profile_dir(profile_dir: str | None) -> Path:
    """The concrete profile path for a request: the caller's, or the default."""
    if profile_dir:
        return Path(profile_dir).expanduser().resolve()
    return _default_profile_dir()


def _backend_for(profile_dir: str | None) -> SeleniumChromeBackend:
    """Build a backend bound to the resolved profile path (empty; no Chrome yet)."""
    return SeleniumChromeBackend(profile_dir=_resolve_profile_dir(profile_dir))


def _tool(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """Turn UnknownTabError into the standard error envelope tools return."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs) -> dict:
        try:
            return await fn(*args, **kwargs)
        except UnknownTabError as e:
            return e.envelope
    return wrapper


# -- navigation tools -------------------------------------------------------

@mcp.tool()
async def list_tabs() -> list[dict]:
    """List all open browser tabs across all profiles' sessions.

    Within a profile this drives the shared focused window like any other tab
    call: issue calls sequentially — concurrent requests (even to different
    ids) race over the focused window and give undefined results.

    Profiles whose Chrome has exited are skipped (they have no open tabs);
    listing never relaunches a browser.
    """
    logger.info("Tool called: list_tabs")

    async def _fetch(session) -> list[dict]:
        if not await session.is_live():
            logger.info(f"list_tabs: skipping dead session (profile={session.profile_dir})")
            return []
        return await session.list_tabs()  # already wire dicts with composite ids

    listings = await asyncio.gather(*(_fetch(s) for s in _store.sessions()))
    logger.info("Tool finished: list_tabs")
    return [tab for tabs in listings for tab in tabs]


@mcp.tool()
async def new_blank_tab(profile_dir: str | None = None) -> dict:
    """Open a new blank tab and return it (navigate it afterwards).

    Optional profile_dir runs the request in an independent Chrome profile; the
    returned id is only valid for that same profile.

    Only one tab can be driven at a time within a profile: interact with tabs
    sequentially — concurrent requests (even to different ids) race over
    the shared focused window and give undefined results.
    """
    logger.info(f"Tool called: new_blank_tab (profile_dir={profile_dir!r})")
    try:
        session = _store.get_or_create_session(_backend_for(profile_dir), max_sessions=_ALLOWLIST.max_browser_sessions)
        result = await session.new_blank_tab(max_tabs=_ALLOWLIST.max_tabs_per_session)  # wire dict with composite id
    except RuntimeError as e:
        return {"error": str(e)}
    logger.info("Tool finished: new_blank_tab")
    return result

@mcp.tool()
@_tool
async def close_tab(id: str) -> dict:
    """Close a tab by id."""
    logger.info(f"Tool called: close_tab (id={id!r})")
    session = _store.route(id)
    await session.close_tab(id)
    logger.info("Tool finished: close_tab")
    return {"closed": id}


@mcp.tool()
@_tool
async def select_tab(id: str) -> dict:
    """Switch the active tab."""
    logger.info(f"Tool called: select_tab (id={id!r})")
    session = _store.route(id)
    await session.select_tab(id)
    logger.info("Tool finished: select_tab")
    return {"selected": id}


@mcp.tool()
@_tool
async def navigate(url: str, id: str) -> dict:
    """Navigate the named tab to url. Url is gated by the per-host allowlist (query strings and fragments pass through)."""
    logger.info(f"Tool called: navigate (url={url!r}, id={id!r})")
    url = validate_url(url, _ALLOWLIST.read_policy)
    session = _store.route(id)
    result = await session.navigate(url, id=id)  # wire dict (or the tab-gone envelope)
    logger.info("Tool finished: navigate")
    return result


# -- write tools ------------------------------------------------------------

@mcp.tool()
@_tool
async def click(css_selector: str, id: str) -> dict:
    """Click a control on a tab — the only write action.

    Three server-side gates, all default-deny, must pass:
      1. The tab's host must be listed under the ``click`` section of the
         allowlist (and not on the denylist). The shipped default has no hosts
         enabled — the amazon.com entry in allowlist.yaml is commented out until
         you opt in.
      2. ``css_selector`` must resolve to exactly one element that is a real,
         visible, non-decoy clickable control (an agent-targeted decoy, a hidden
         or disabled element, or a non-clickable tag is refused). This gate
         judges element *integrity*, not intent.
      3. The control's visible text must fully match the host's required
         ``label`` regex. *What* a control may do is defined here, by the
         operator — a host that wants to permit any control states it
         explicitly as ``label: '.*'`` (an omitted label fails config parsing).
    Any gate failing raises a ValidationError and nothing is clicked.
    """
    logger.info(f"Tool called: click (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)

    # Gate 1: per-action host allowlist, checked against the tab's live URL.
    # The denylist vetoes first (a denied host is never clickable, even if the
    # human opened it), then the click section's own host allowlist must pass.
    url = await session.current_url(id=id)
    if url is None:
        return {"error": f"tab {id} is no longer open — call list_tabs for current tabs",
                "id": id}
    parsed = urlparse(url)
    if _ALLOWLIST.is_denied(parsed.hostname or "", parsed.path):
        raise ValidationError(f"URL on denylist: {parsed.hostname}{parsed.path}")
    validate_url(url, _ALLOWLIST.section("click"))  # raises if host not allowed

    # Gate 2: the element must be a single, real, visible, non-decoy control.
    found = await session.query_selector_all(css_selector, id=id, limit=2)
    if "error" in found:
        return found
    total = found["total_count"]
    if total == 0:
        raise ValidationError(f"no element matches selector {css_selector!r}")
    if total > 1:
        raise ValidationError(f"selector {css_selector!r} is ambiguous ({total} matches) — refusing to click")
    node = found["elements"][0]
    if not is_clickable_control(node):
        raise ValidationError(
            "selected element is not a clickable control (or is a hidden/disabled/decoy element) — refusing to click")

    # Gate 3: the host's required label. Every listed host has one (config
    # parsing enforces it); '.*' is how a host opts into any control. Fail
    # closed if it is somehow absent rather than waving the click through.
    host = urlparse(url).hostname or ""
    label_re = _ALLOWLIST.label_pattern("click", host)
    if label_re is None or not label_matches(node, label_re):
        raise ValidationError(
            f"control text does not match the required label for {host} — refusing to click")

    result = await session.click(css_selector, id=id)
    logger.info("Tool finished: click")
    return result


@mcp.tool()
@_tool
async def insert_text(css_selector: str, value: str, id: str) -> dict:
    """Type text into a field on a tab — the write-text action.

    The text-entry counterpart of ``click``. Three server-side gates, all
    default-deny, must pass:
      1. The tab's host must be listed under the ``write-text`` section of the
         allowlist (and not on the denylist) — a section *separate* from
         ``click``, so permitting typing never implies permitting clicks, or the
         reverse.
      2. ``css_selector`` must resolve to exactly one element that is a real,
         visible, non-decoy, non-readonly text control (a ``<textarea>``, a
         text-like ``<input>``, or a ``contenteditable`` element). Integrity, not
         intent.
      3. The field's *visible label* — its placeholder / aria-label /
         aria-labelledby / associated ``<label>`` / title — must fully match the
         host's required ``write-text`` ``label`` regex, so the operator
         authorizes *which* boxes may be typed into by the name a human reads next
         to them (never a hidden ``name``/``id``). ``label: '.*'`` opts into any.
    Any gate failing raises a ValidationError and nothing is typed.
    """
    logger.info(f"Tool called: insert_text (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)

    # Gate 1: per-action host allowlist ('write-text'); denylist vetoes first.
    url = await session.current_url(id=id)
    if url is None:
        return {"error": f"tab {id} is no longer open — call list_tabs for current tabs",
                "id": id}
    parsed = urlparse(url)
    if _ALLOWLIST.is_denied(parsed.hostname or "", parsed.path):
        raise ValidationError(f"URL on denylist: {parsed.hostname}{parsed.path}")
    validate_url(url, _ALLOWLIST.section("write-text"))  # raises if host not allowed

    # Gate 2: the element must be a single, real, visible, non-decoy text box.
    found = await session.query_selector_all(css_selector, id=id, limit=2)
    if "error" in found:
        return found
    total = found["total_count"]
    if total == 0:
        raise ValidationError(f"no element matches selector {css_selector!r}")
    if total > 1:
        raise ValidationError(f"selector {css_selector!r} is ambiguous ({total} matches) — refusing to insert text")
    node = found["elements"][0]
    if not is_fillable_control(node):
        raise ValidationError(
            "selected element is not a fillable text control (or is a "
            "hidden/disabled/readonly/decoy element) — refusing to insert text")

    # Gate 3: the host's required write-text label, matched against the field's
    # visible label. Fail closed if it is somehow absent.
    host = parsed.hostname or ""
    label_re = _ALLOWLIST.label_pattern("write-text", host)
    if label_re is None or not field_label_matches(node, label_re):
        raise ValidationError(
            f"field label does not match the required write-text label for {host} — refusing to insert text")

    result = await session.insert_text(css_selector, value, id=id)
    logger.info("Tool finished: insert_text")
    return result


# -- DOM-query tools --------------------------------------------------------
#
# These take a REQUIRED id (the id from new_blank_tab / navigate / list_tabs).
# Like navigate / select_page / force_reload_page, they never default to "the
# active tab": the active tab is shared state the human also controls, so an
# implicit default would silently act on whichever tab happens to be focused.
# A id that no longer names an open tab comes back as
# {"error": ..., "id": ...}.

@mcp.tool()
@_tool
async def get_element_by_id(element_id: str, id: str,
                            include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.getElementById on a tab — one element node, or found=false (not an error) if absent."""
    logger.info(f"Tool called: get_element_by_id (element_id={element_id!r}, id={id!r})")
    session = _store.route(id)
    result = await session.get_element_by_id(
        element_id, id=id, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_element_by_id")
    return result


@mcp.tool()
@_tool
async def get_elements_by_class_name(class_names: str, id: str,
                                     limit: int = 10, offset: int = 0,
                                     include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.getElementsByClassName on a tab — space-separated names, element must have ALL. Paginated."""
    logger.info(f"Tool called: get_elements_by_class_name (class_names={class_names!r}, id={id!r})")
    session = _store.route(id)
    result = await session.get_elements_by_class_name(
        class_names, id=id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_elements_by_class_name")
    return result


@mcp.tool()
@_tool
async def query_selector(css_selector: str, id: str,
                         include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelector on a tab — one element node, or found=false if no match. Invalid CSS → error."""
    logger.info(f"Tool called: query_selector (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)
    result = await session.query_selector(
        css_selector, id=id, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: query_selector")
    return result


@mcp.tool()
@_tool
async def query_selector_all(css_selector: str, id: str,
                             limit: int = 10, offset: int = 0,
                             include_html: bool = False, max_html_bytes: int = 4096) -> dict:
    """document.querySelectorAll on a tab — paginated list of element nodes. Invalid CSS → error."""
    logger.info(f"Tool called: query_selector_all (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)
    result = await session.query_selector_all(
        css_selector, id=id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: query_selector_all")
    return result


@mcp.tool()
@_tool
async def screenshot(id: str):  # -> dict | Image; unannotated: FastMCP can't schema-ify Image
    """Capture a PNG screenshot of a tab's current viewport.

    Read-only: it grabs live pixels from the rendered page and never mutates it
    or the DOM cache. Returns the image on success, or
    ``{"error": ..., "id": ...}`` if the tab is no longer open.
    """
    logger.info(f"Tool called: screenshot (id={id!r})")
    session = _store.route(id)
    result = await session.screenshot(id=id)
    if isinstance(result, dict):  # tab gone — structured error, not an image
        return result
    logger.info("Tool finished: screenshot")
    return Image(data=result, format="png")


@mcp.tool()
@_tool
async def force_reload_tab(id: str) -> dict:
    """Reload the named tab and refresh its cached DOM."""
    logger.info(f"Tool called: force_reload_page (id={id!r})")
    session = _store.route(id)
    result = await session.force_reload_tab(id=id)
    logger.info("Tool finished: force_reload_page")
    return result


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: resolve + load the allowlist, then serve.

    The loaded, schema-verified allowlist replaces the module default so every
    tool (the internal consumers of ``_ALLOWLIST``) gates against the config
    the operator chose.
    """
    global _ALLOWLIST
    parser = argparse.ArgumentParser(
        prog="browden", description="Browden MCP server")
    parser.add_argument(
        "--allowlist",
        help="Path to the allowlist YAML config (default: $BROWDEN_ALLOWLIST, "
             "then ~/.browden/allowlist.yaml, then the repo sample)")
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
