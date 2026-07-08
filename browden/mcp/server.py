import argparse
import asyncio
import functools
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse


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

from .validator import (
    ActionAllowlist,
    ValidationError,
    check_action_host,
    validate_click_target,
    validate_url,
    validate_write_text_target,
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


def _default_cache_root(platform: str = sys.platform, os_name: str = os.name) -> Path:
    """Per-OS cache root for browden's shared state.

    An explicit ``XDG_CACHE_HOME`` wins on every platform (tests and power users
    rely on it); otherwise use each OS's idiomatic cache location — macOS
    ``~/Library/Caches``, Windows ``%LOCALAPPDATA%``, Linux ``~/.cache``.

    ``platform``/``os_name`` are injectable so the per-OS branches are testable
    from any host without perturbing ``os.name`` (which flips pathlib's flavour).
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg)
    if platform == "darwin":
        return Path.home() / "Library" / "Caches"
    if os_name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    return Path.home() / ".cache"


def _default_profile_dir() -> Path:
    """The shared default Chrome profile path (see ``_default_cache_root``).

    Lives here, not in the backend: the backend never falls back to a default —
    the server is the caller that decides which profile, and hands the backend a
    concrete path.
    """
    return _default_cache_root() / "browden" / "chrome-profile"


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


def _read_ok(url: str) -> bool:
    """Whether the read policy admits ``url`` — the reading counterpart of the
    gate navigate() applies. Used to stop the DOM-read/screenshot tools acting on
    a tab that sits on a non-allowlisted site (finding H2)."""
    p = urlparse(url)
    return _ALLOWLIST.read_policy.is_allowed(p.hostname or "", p.path)


async def _gate_tab_read(session, id: str) -> "dict | None":
    """Refuse a read tool when the tab's LIVE url isn't on the read allowlist.

    navigate() gates where the agent may *go*, but the DOM-query, screenshot and
    reload tools acted on whichever tab was focused with no policy check at all —
    so any tab the human (or a redirect) parked on a non-allowlisted site (a bank,
    webmail, an internal app) could still be scraped or screenshotted and its
    content fed to the model (finding H2). Re-check the tab's live url here so the
    read allowlist governs *reading*, not only navigation.

    Returns the tab-gone envelope if the tab has closed, ``None`` when the read
    may proceed, and raises :class:`ValidationError` (same refusal shape as
    navigate/click) when the tab's host is not on the read allowlist.
    """
    url = await session.current_url(id=id)
    if url is None:
        return {"error": f"tab {id} is no longer open — call list_tabs for current tabs", "id": id}
    if not _read_ok(url):
        host = urlparse(url).hostname or url
        raise ValidationError(
            f"tab {id} is on {host!r}, which the read allowlist does not permit — refusing to read it")
    return None


def _redact_unlisted(tab: dict) -> dict:
    """Blank the url/title of a listed tab whose host isn't on the read allowlist.

    list_tabs aggregates every tab across every profile, so without this it leaks
    the urls/titles of whatever non-allowlisted sites the human has open (H2). The
    id/selected/profile_dir survive so the agent can still manage the tab (select
    it, navigate it elsewhere, close it) — it just can't see where it sits."""
    if _read_ok(tab.get("url") or ""):
        return tab
    return {**tab, "url": "<hidden: not on read allowlist>",
            "title": "<hidden: not on read allowlist>"}


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
        # Redact tabs on non-allowlisted hosts so the listing can't leak the
        # human's other open sites (H2); the id survives so they stay manageable.
        return [_redact_unlisted(t) for t in await session.list_tabs()]

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
         visible, non-decoy clickable control — a ``<button>``, ``role="button"``,
         ``<input type=submit|button>``, or an ``<a>`` anchor (an agent-targeted
         decoy, or a hidden/disabled element, is refused). This gate judges
         element *integrity*, not intent. For an anchor there is one extra check:
         where its href would navigate must itself be on the read allowlist (the
         same gate as ``navigate``) — relative and ``javascript:`` hrefs stay in
         place, a cross-domain href is allowed only if that site is allow-listed,
         and other schemes (``mailto:``/``tel:``/…) are refused — so a "click"
         can't be a disguised jump to a site you couldn't navigate to.
      3. The control's visible text must fully match the host's required
         ``label`` regex. *What* a control may do is defined here, by the
         operator — a host that wants to permit any control states it
         explicitly as ``label: '.*'`` (an omitted label fails config parsing).
    Any gate failing raises a ValidationError and nothing is clicked.
    """
    logger.info(f"Tool called: click (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)

    # Gate 1: per-action host allowlist (denylist veto + the click section),
    # checked against the tab's live URL before the element is ever queried.
    url = await session.current_url(id=id)
    if url is None:
        return {"error": f"tab {id} is no longer open — call list_tabs for current tabs",
                "id": id}
    check_action_host(_ALLOWLIST, "click", url)  # raises if denied / host not allowed

    # Gates 2-3: fetch the element (limit=2 so ambiguity is detectable), then let
    # the validator judge integrity, anchor target, and the host's required label.
    found = await session.query_selector_all(css_selector, id=id, limit=2)
    if "error" in found:
        return found
    validate_click_target(_ALLOWLIST, url, css_selector, found)  # raises on any failed gate

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
         to them. ``label: '.*'`` opts into any. As an explicit escape hatch for a
         box with *no* visible label, the host's ``field_ids`` may instead name it
         by exact ``id``/``name``; the field passes if the label OR an id matches.
    Any gate failing raises a ValidationError and nothing is typed.
    """
    logger.info(f"Tool called: insert_text (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)

    # Gate 1: per-action host allowlist ('write-text'; denylist vetoes first),
    # checked against the tab's live URL before the element is ever queried.
    url = await session.current_url(id=id)
    if url is None:
        return {"error": f"tab {id} is no longer open — call list_tabs for current tabs",
                "id": id}
    check_action_host(_ALLOWLIST, "write-text", url)  # raises if denied / host not allowed

    # Gates 2-3: fetch the element (limit=2 so ambiguity is detectable), then let
    # the validator judge integrity and the host's required label / field-id.
    found = await session.query_selector_all(css_selector, id=id, limit=2)
    if "error" in found:
        return found
    validate_write_text_target(_ALLOWLIST, url, css_selector, found)  # raises on any failed gate

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
    gone = await _gate_tab_read(session, id)  # H2: read gate on the tab's live url
    if gone is not None:
        return gone
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
    gone = await _gate_tab_read(session, id)  # H2: read gate on the tab's live url
    if gone is not None:
        return gone
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
    gone = await _gate_tab_read(session, id)  # H2: read gate on the tab's live url
    if gone is not None:
        return gone
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
    gone = await _gate_tab_read(session, id)  # H2: read gate on the tab's live url
    if gone is not None:
        return gone
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
    gone = await _gate_tab_read(session, id)  # H2: read gate on the tab's live url
    if gone is not None:
        return gone
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
    gone = await _gate_tab_read(session, id)  # H2: read gate on the tab's live url
    if gone is not None:
        return gone
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
