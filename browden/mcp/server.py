import argparse
import asyncio
import contextlib
import functools
import inspect
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse


from ..common.logger import logger
from ..configs.loader import (
    AllowlistRefresher,
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
    ensure_url_allowed,
    tab_gone_envelope,
    validate_click_target,
    validate_url,
    validate_write_text_target,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_INSTRUCTIONS = (
    "browden drives a real Chrome session. Within a single profile-dir there is ONE browser session. You can open multiple tabs within that one session, though concurrent requests are only supported across different sessions, but within the same session (this is a limitation of selenium: the underlying automation library). A new profile-dir can be chosen while crating a new tab. If you choose a previously used profile-dir, then the previous session will be reused. Creating a new tab will return a tab-id which is unique across all sessions, pass it back verbatim on other tools."
)


@contextlib.asynccontextmanager
async def _allowlist_lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Run the live allowlist's hot-reload poller for the server's lifetime.

    Delegates to the refresher (see ``configs/loader/refresher.py``): the poller
    lives on the *same* event loop as the tools, started on boot and cancelled
    cleanly on shutdown.
    """
    async with _refresher.run():
        yield


mcp = FastMCP(
    "browden",
    instructions=_INSTRUCTIONS,
    host=os.environ.get("MCP_HOST", DEFAULT_HOST),
    port=int(os.environ.get("MCP_PORT", DEFAULT_PORT)),
    lifespan=_allowlist_lifespan,
)

# Import-time default: a *static* refresher over the repo sample (reads open,
# writes deny-all), so unit tests and library imports see a deterministic policy
# and watch no file. main() re-resolves (CLI > env > user config > sample) and
# replaces this with a file-watching refresher before serving. Every tool reads
# the live policy off ``_refresher.allowlist``, which the refresher hot-reloads
# in place via a lock-free atomic swap (see configs/loader/refresher.py).
_refresher = AllowlistRefresher.static(
    load_allowlist(SAMPLE_ALLOWLIST) if SAMPLE_ALLOWLIST.exists() else ActionAllowlist({}))

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


# insert_text's typed content may be sensitive (card/CVV/etc.), so the call log
# names the field but never the value — every other tool arg is fair to log.
_UNLOGGED_ARGS = frozenset({"value"})


def _logged(fn: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
    """Bracket a tool call with the ``Tool called``/``Tool finished`` log lines.

    Replaces the hand-written pair every tool used to open and close with. The
    call line names the tool and reprs its arguments (minus :data:`_UNLOGGED_ARGS`);
    the finish line fires only on a normal return, so an exception (a failed gate)
    leaves just the call line — same as the old hand-rolled logging.
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            bound = sig.bind(*args, **kwargs)
            shown = ", ".join(f"{k}={v!r}" for k, v in bound.arguments.items()
                              if k not in _UNLOGGED_ARGS)
        except TypeError:
            shown = ""  # bad call (e.g. missing id) — let the body raise the real error
        logger.info(f"Tool called: {fn.__name__} ({shown})")
        result = await fn(*args, **kwargs)
        logger.info(f"Tool finished: {fn.__name__}")
        return result

    return wrapper


def _tab_gate(check: Callable[[str], None]):
    """Build a decorator enforcing a per-tab URL policy before the tool body runs.

    Collapses the read/host-gate preamble every DOM and write tool repeated: route
    the id to its session, read the tab's *live* URL, short-circuit to the tab-gone
    envelope if the tab is closed, then run ``check(url)`` (which raises to deny).
    The tool body may declare keyword-only ``session`` / ``url`` parameters to
    receive the already-routed session and that live URL, so it re-routes nothing
    and re-reads nothing. Those two are server-injected, so they're stripped from
    the signature the MCP schema (and the caller) sees.
    """
    def decorator(fn):
        sig = inspect.signature(fn)
        injected = [n for n in ("session", "url") if n in sig.parameters]
        public = sig.replace(
            parameters=[p for n, p in sig.parameters.items() if n not in injected])

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> dict:
            bound = public.bind(*args, **kwargs)
            bound.apply_defaults()
            id = bound.arguments["id"]
            session = _store.route(id)
            url = await session.current_url(id=id)
            if url is None:
                return tab_gone_envelope(id)
            check(url)  # raises (ValidationError / host gate) to deny
            available = {"session": session, "url": url}
            return await fn(**bound.arguments, **{n: available[n] for n in injected})

        wrapper.__signature__ = public  # hide the injected params from the MCP schema
        return wrapper

    return decorator


def _read_check(url: str) -> None:
    """H2: refuse a read of a tab whose live URL isn't on the read allowlist."""
    if not ensure_url_allowed(_refresher.allowlist, url):
        raise ValidationError(f"URL not on the read allowlist: {url}")


read_gated = _tab_gate(_read_check)


def host_gated(action: str):
    """Gate a write tool (``click`` / ``write-text``) on the tab's live host.

    The per-action counterpart of :data:`read_gated`: the tab's host must be
    listed under ``action`` (denylist vetoes first), checked against the live URL
    before the element is ever queried.
    """
    return _tab_gate(lambda url: check_action_host(_refresher.allowlist, action, url))


# -- navigation tools -------------------------------------------------------

@mcp.tool()
@_logged
async def list_tabs() -> list[dict]:
    """List all open browser tabs across all profiles' sessions.

    Within a profile this drives the shared focused window like any other tab
    call: issue calls sequentially — concurrent requests (even to different
    ids) race over the focused window and give undefined results.

    Profiles whose Chrome has exited are skipped (they have no open tabs);
    listing never relaunches a browser.
    """
    async def _fetch(session) -> list[dict]:
        if not await session.is_live():
            logger.info(f"list_tabs: skipping dead session (profile={session.profile_dir})")
            return []
        # H2: a tab on a non-allowlisted host is closed, not just hidden — the
        # agent can neither read it nor learn it exists. The same read gate the
        # DOM tools use decides: if it fails, close the tab (best-effort — the
        # last tab can't be closed) and drop it from the listing.
        kept: list[dict] = []
        for tab in await session.list_tabs():
            if ensure_url_allowed(_refresher.allowlist, tab.get("url") or ""):
                kept.append(tab)
                continue
            logger.warning(f"list_tabs: closing non-allowlisted tab {tab.get('url')!r} (id={tab.get('id')})")
            try:
                await session.close_tab(tab["id"])
            except Exception as e:
                logger.warning(f"list_tabs: could not close tab {tab.get('id')}: {e}")
        return kept

    listings = await asyncio.gather(*(_fetch(s) for s in _store.sessions()))
    return [tab for tabs in listings for tab in tabs]


@mcp.tool()
@_logged
async def new_blank_tab(profile_dir: str | None = None) -> dict:
    """Open a new blank tab and return it (navigate it afterwards).

    Optional profile_dir runs the request in an independent Chrome profile; the
    returned id is only valid for that same profile.

    Only one tab can be driven at a time within a profile: interact with tabs
    sequentially — concurrent requests (even to different ids) race over
    the shared focused window and give undefined results.
    """
    try:
        session = _store.get_or_create_session(_backend_for(profile_dir), max_sessions=_refresher.allowlist.max_browser_sessions)
        result = await session.new_blank_tab(max_tabs=_refresher.allowlist.max_tabs_per_session)  # wire dict with composite id
    except RuntimeError as e:
        return {"error": str(e)}
    return result


@mcp.tool()
@_tool
@_logged
async def close_tab(id: str) -> dict:
    """Close a tab by id."""
    session = _store.route(id)
    await session.close_tab(id)
    return {"closed": id}


@mcp.tool()
@_tool
@_logged
async def select_tab(id: str) -> dict:
    """Switch the active tab."""
    session = _store.route(id)
    await session.select_tab(id)
    return {"selected": id}


async def _guard_landing(session, id: str, result: dict) -> dict:
    """Re-check where a navigation/reload actually came to rest.

    ``validate_url`` only gates the *input* URL, but ``drv.get``/``refresh``
    follow 3xx / meta / JS redirects to any final URL — an open redirect on an
    allowlisted site, or a server-side 302, can land the tab on an unchecked
    host. Re-gate the landing (``result["url"]``) with the *same* ``validate_url``
    the navigate input passed through, so it is judged by exactly the same policy
    on the way out as on the way in. A landing that fails — an off-allowlist host
    or a scheme the policy doesn't admit (``chrome://`` / ``data:`` / ``blob:`` /
    …) — bounces the tab to ``about:blank`` and returns an error envelope rather
    than leaving it silently parked off-list.

    ``about:blank`` needs no special-case here: ``validate_url`` allows it
    explicitly (it is the inert empty state and our own bounce target), so a tab
    that legitimately rests there re-gates clean.
    """
    landed = result.get("url")
    if not landed:
        return result  # a tab-gone envelope or similar — nothing navigated
    try:
        validate_url(landed, _refresher.allowlist.read_policy)
    except ValidationError:
        logger.warning(f"navigation landed off-allowlist at {landed!r}; bouncing to about:blank")
        await session.navigate("about:blank", id=id)
        return {"error": f"navigation left the allowlist (landed on {landed}) — "
                         f"tab reset to about:blank",
                "id": id, "url": landed}
    return result


@mcp.tool()
@_tool
@_logged
async def navigate(url: str, id: str) -> dict:
    """Navigate the named tab to url. Url is gated by the per-host allowlist (query strings and fragments pass through)."""
    url = validate_url(url, _refresher.allowlist.read_policy)
    session = _store.route(id)
    result = await session.navigate(url, id=id)  # wire dict (or the tab-gone envelope)
    result = await _guard_landing(session, id, result)  # re-gate the post-redirect landing
    return result


# -- write tools ------------------------------------------------------------

@mcp.tool()
@_tool
@_logged
@host_gated("click")
async def click(css_selector: str, id: str, *, session, url) -> dict:
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
    # Gate 1 (the tab's live host is listed under `click`) is enforced by
    # @host_gated before we get here; it hands us the routed session and live url.
    # Gates 2-3: fetch the element (limit=2 so ambiguity is detectable), then let
    # the validator judge integrity, anchor target, and the host's required label.
    found = await session.query_selector_all(css_selector, id=id, limit=2)
    if "error" in found:
        return found
    validate_click_target(_refresher.allowlist, url, css_selector, found)  # raises on any failed gate
    return await session.click(css_selector, id=id)


@mcp.tool()
@_tool
@_logged
@host_gated("write-text")
async def insert_text(css_selector: str, value: str, id: str, *, session, url) -> dict:
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
    # Gate 1 (the tab's live host is listed under `write-text`) is enforced by
    # @host_gated before we get here; it hands us the routed session and live url.
    # Gates 2-3: fetch the element (limit=2 so ambiguity is detectable), then let
    # the validator judge integrity and the host's required label / field-id.
    found = await session.query_selector_all(css_selector, id=id, limit=2)
    if "error" in found:
        return found
    validate_write_text_target(_refresher.allowlist, url, css_selector, found)  # raises on any failed gate
    return await session.insert_text(css_selector, value, id=id)


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
@_logged
@read_gated
async def get_element_by_id(element_id: str, id: str,
                            include_html: bool = False, max_html_bytes: int = 4096,
                            *, session) -> dict:
    """document.getElementById on a tab — one element node, or found=false (not an error) if absent."""
    return await session.get_element_by_id(
        element_id, id=id, include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
@_tool
@_logged
@read_gated
async def get_elements_by_class_name(class_names: str, id: str,
                                     limit: int = 10, offset: int = 0,
                                     include_html: bool = False, max_html_bytes: int = 4096,
                                     *, session) -> dict:
    """document.getElementsByClassName on a tab — space-separated names, element must have ALL. Paginated."""
    return await session.get_elements_by_class_name(
        class_names, id=id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
@_tool
@_logged
@read_gated
async def query_selector(css_selector: str, id: str,
                         include_html: bool = False, max_html_bytes: int = 4096,
                         *, session) -> dict:
    """document.querySelector on a tab — one element node, or found=false if no match. Invalid CSS → error."""
    return await session.query_selector(
        css_selector, id=id, include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
@_tool
@_logged
@read_gated
async def query_selector_all(css_selector: str, id: str,
                             limit: int = 10, offset: int = 0,
                             include_html: bool = False, max_html_bytes: int = 4096,
                             *, session) -> dict:
    """document.querySelectorAll on a tab — paginated list of element nodes. Invalid CSS → error."""
    return await session.query_selector_all(
        css_selector, id=id, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)


@mcp.tool()
@_tool
@_logged
@read_gated
async def screenshot(id: str, *, session):  # -> dict | Image; unannotated: FastMCP can't schema-ify Image
    """Capture a PNG screenshot of a tab's current viewport.

    Read-only: it grabs live pixels from the rendered page and never mutates it
    or the DOM cache. Returns the image on success, or
    ``{"error": ..., "id": ...}`` if the tab is no longer open.
    """
    result = await session.screenshot(id=id)
    if isinstance(result, dict):  # tab gone — structured error, not an image
        return result
    return Image(data=result, format="png")


@mcp.tool()
@_tool
@_logged
@read_gated
async def force_reload_tab(id: str, *, session) -> dict:
    """Reload the named tab and refresh its cached DOM."""
    result = await session.force_reload_tab(id=id)
    return await _guard_landing(session, id, result)  # a reload can 302 off-list too


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: resolve + load the allowlist, then serve.

    Replaces the module default with a file-watching refresher over the config
    the operator chose, so every tool (the internal consumers of
    ``_refresher.allowlist``) gates against it — and picks up edits live, since
    ``_allowlist_lifespan`` runs the refresher's poller while the server serves.
    """
    global _refresher
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
        _refresher = AllowlistRefresher.from_path(path)
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
