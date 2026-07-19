import argparse
import asyncio
import contextlib
import functools
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated


from ..common.logger import logger
from ..common.profile import canonical_profile_dir
from ..configs.loader import (
    RuntimeConfigurationRefresher,
    ConfigError,
    DEFAULT_RELOAD_INTERVAL_SECONDS,
    SAMPLE_ALLOWLIST,
    load_runtime_configuration,
    resolve_allowlist_path,
)
from ..dependencies.mcp import FastMCP, Image
from ..dependencies.pydantic import Field
from ..web_navigator.selenium_chrome import SeleniumChromeBackend
from .session_management.browser_session_store import BrowserSessionStore, UnknownTabError

from .validator import (
    BrowdenAccessRuleSet,
    BrowdenRuntimeConfiguration,
    SessionBusyError,
    ValidationError,
    click_gate,
    press_key_gate,
    read_gate,
    tab_gone_envelope,
    upload_file_gate,
    validate_and_ensure_same_origin,
    validate_url,
    write_text_gate,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_INSTRUCTIONS = (
    "browden drives a real Chrome session. Within a single profile-dir there is ONE browser session. You can open multiple tabs within that one session, and you may fire concurrent requests at them: selenium (the underlying automation library) is not thread-safe, so a session serves its requests one at a time behind a lock, each re-selecting its own tab when its turn comes. A request that waits more than 10s for its turn comes back with a 'browser session busy' error; retry it. A new profile-dir can be chosen while creating a new tab. If you choose a previously used profile-dir, then the previous session will be reused. Creating a new tab will return a tab-id which is unique across all sessions, pass it back verbatim on other tools. A tab must be selected before any operation acts on it: passing a tool the tab's id selects that tab, and only one tab per session can be selected at a time."
)


@contextlib.asynccontextmanager
async def _runtime_configuration_lifespan(_server: FastMCP) -> AsyncIterator[None]:
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
    lifespan=_runtime_configuration_lifespan,
)

# Import-time default: a *static* refresher over the repo sample (reads open,
# writes deny-all), so unit tests and library imports see a deterministic policy
# and watch no file. main() re-resolves (CLI > env > user config > sample) and
# replaces this with a file-watching refresher before serving. Every tool reads
# the live policy off ``_refresher.runtime_configuration``, which the refresher hot-reloads
# in place via a lock-free atomic swap (see configs/loader/refresher.py).
_refresher = RuntimeConfigurationRefresher.static(
    load_runtime_configuration(SAMPLE_ALLOWLIST) if SAMPLE_ALLOWLIST.exists() else BrowdenRuntimeConfiguration({}))

logger.info("Browden MCP module initialized")

# All per-profile session state and the customer<->backend id mapping live in
# the store (see session_management/browser_session_store.py).
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
    return canonical_profile_dir(_default_cache_root() / "browden" / "chrome-profile")


def _resolve_profile_dir(profile_dir: str | None) -> Path:
    """The concrete profile path for a request: the caller's, or the default.

    Canonicalized through the shared :func:`canonical_profile_dir`, so a path a
    caller spells one way names the same profile as the same path spelled
    another way — here and anywhere else a profile is named.
    """
    if profile_dir:
        return canonical_profile_dir(profile_dir)
    return _default_profile_dir()


def _access_rules_for(session) -> BrowdenAccessRuleSet:
    """The rule set that gates a request: the live rules, scoped to its profile.

    Every gate in this module is handed this and nothing else. The configuration
    is read off ``_refresher`` at call time (so a hot reload lands on the very
    next request), and scoped to the profile ``session`` drives — its own rules
    if the config gives it a ``profiles`` entry, the global ones otherwise.
    """
    return _refresher.runtime_configuration.access_rules_for(session.profile_dir)


def _backend_for(profile_dir: str | None) -> SeleniumChromeBackend:
    """Build a backend bound to the resolved profile path (empty; no Chrome yet)."""
    return SeleniumChromeBackend(profile_dir=_resolve_profile_dir(profile_dir))


def _tool(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """Turn UnknownTabError / SessionBusyError into the error envelopes tools return.

    ``SessionBusyError`` means this request waited out the driver-lock timeout
    behind other requests on the same profile (see ``BrowserSessionManager``);
    the tab it names is fine, so the envelope echoes the ``id`` back the way the
    tab-gone one does, and the agent can simply retry.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs) -> dict:
        try:
            return await fn(*args, **kwargs)
        except UnknownTabError as e:
            return e.envelope
        except SessionBusyError as e:
            # FastMCP calls tools with keyword arguments, so the tab's id (if the
            # tool takes one) is in kwargs.
            id = kwargs.get("id")
            return e.envelope if id is None else {**e.envelope, "id": id}
    return wrapper


# -- navigation tools -------------------------------------------------------

@mcp.tool()
async def list_tabs() -> list[dict]:
    """List all open browser tabs across all profiles' sessions.

    Within a profile this drives the shared focused window like any other tab
    call, so it queues behind whatever else that profile is doing; concurrent
    calls are safe, they are just served one at a time per profile.

    Profiles whose Chrome has exited are skipped (they have no open tabs);
    listing never relaunches a browser.
    """
    logger.info("Tool called: list_tabs")

    async def _fetch(session) -> list[dict]:
        try:
            return await _fetch_profile(session)
        except SessionBusyError as e:
            # One profile too busy to answer must not sink the whole listing, and
            # must not look like "that profile has no tabs" either — report it in
            # place of its tabs so the agent knows to retry rather than assuming
            # the tabs are gone.
            logger.warning(f"list_tabs: session busy (profile={session.profile_dir})")
            return [{"error": str(e), "profile_dir": session.profile_dir}]

    async def _fetch_profile(session) -> list[dict]:
        if not await session.is_live():
            logger.info(f"list_tabs: skipping dead session (profile={session.profile_dir})")
            return []
        # H2: the session closes any tab parked off the read allowlist and leaves
        # it out of the listing, in the same driver hold as the listing itself.
        return await session.list_tabs(gate=read_gate(_access_rules_for(session)))

    listings = await asyncio.gather(*(_fetch(s) for s in _store.sessions()))
    logger.info("Tool finished: list_tabs")
    return [tab for tabs in listings for tab in tabs]


@mcp.tool()
async def new_blank_tab(profile_dir: str | None = None) -> dict:
    """Open a new blank tab and return it (navigate it afterwards).

    Optional profile_dir runs the request in an independent Chrome profile; the
    returned id is only valid for that same profile.

    Only one tab can be driven at a time within a profile: concurrent requests
    to a profile's tabs are safe but are served one after another (each
    re-selects its own tab), so they do not run in parallel. Use separate
    profile_dirs for genuine parallelism.
    """
    logger.info(f"Tool called: new_blank_tab (profile_dir={profile_dir!r})")
    try:
        session = _store.get_or_create_session(
            _backend_for(profile_dir),
            max_sessions=_refresher.runtime_configuration.max_browser_sessions,
            # A getter, not a value: the session's reaper re-reads it every tick,
            # so an infra.reap_interval_seconds edit lands without a restart.
            reap_interval_seconds=lambda: _refresher.runtime_configuration.reap_interval_seconds)
        result = await session.new_blank_tab(max_tabs=_refresher.runtime_configuration.max_tabs_per_session)  # wire dict with composite id
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
    result = await session.select_tab(id)  # {"selected": id}, or the tab-gone envelope
    logger.info("Tool finished: select_tab")
    return result


@mcp.tool()
@_tool
async def navigate(url: str, id: str) -> dict:
    """Navigate the named tab to url. Url is gated by the per-host allowlist (query strings and fragments pass through)."""
    logger.info(f"Tool called: navigate (url={url!r}, id={id!r})")
    session = _store.route(id)  # routed first: the profile decides the rules
    access_rules = _access_rules_for(session)  # read once for the whole request
    url = validate_url(url, access_rules.read_policy)
    # The session re-gates wherever the navigation actually landed (a redirect can
    # go anywhere) and bounces an off-list landing, all in the navigate's own hold.
    result = await session.navigate(url, id=id, gate=read_gate(access_rules))
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
         element *integrity*, not intent. A ``<label>`` bound to a radio or
         checkbox also counts: pages routinely hide the native input in CSS and
         draw the visible control on the label, so the label is the only thing a
         human — or this tool — can click. Target it directly
         (``label[for="stage2"]``); the label's own visible text is what gate 3
         matches, and the control it activates must be a real, enabled,
         non-decoy radio/checkbox. For an anchor there is one extra check:
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
    # The rules are read once; the session runs every gate and the click in one
    # driver-lock hold (see docs/design/gate-atomicity.md).
    result = await session.click(css_selector, id=id, gate=click_gate(_access_rules_for(session)))
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
    # The rules are read once; the session runs every gate and the typing in one
    # driver-lock hold (see docs/design/gate-atomicity.md).
    result = await session.insert_text(css_selector, value, id=id,
                                       gate=write_text_gate(_access_rules_for(session)))
    logger.info("Tool finished: insert_text")
    return result


@mcp.tool()
@_tool
async def upload_file(css_selector: str, file_path: str, id: str) -> dict:
    """Attach a local file to a file input on a tab — the upload-file action.

    How a receipt gets onto an expense, a document onto a form. It sets the
    ``<input type="file">`` directly, which is what a human's file-picker
    selection does to the page; clicking the control would open the operating
    system's own file dialog, which is not part of the page and which browden
    cannot drive.

    A section of its own, never part of ``write-text``: reading a file off this
    machine and handing it to a website is a different capability from typing
    into a box, so a host trusted with typing is not thereby trusted with the
    filesystem. Four server-side gates, all default-deny, must pass:
      1. The tab's host must be listed under the ``upload-file`` section of the
         allowlist (and not on the denylist), with a rule whose page selector
         matches this page.
      2. ``css_selector`` must resolve to exactly one element that is a real,
         visible, non-decoy, non-readonly ``<input type="file">``. Integrity, not
         intent. A ``multiple`` input is refused for now: this action uploads one
         file, and that is the only control that could hold a second.
      3. That control must be authorized by a matching rule — by its visible
         label, or by its exact ``id``/``name`` in the rule's ``field_ids``. The
         id half matters more here than for text: a file input routinely carries
         no visible label at all, so no label regex could ever match it.
      4. ``file_path`` must resolve — through ``~``, ``..`` and every symlink —
         to an existing regular file under one of the operator's configured
         ``allowed_upload_locations``, within the size cap, and may contain no
         control character (the driver splits a path on newlines, so one would
         name a second file). The path the gate resolves is the one the browser
         is handed; it is never resolved a second time. **With no ``allowed_upload_locations``
         configured nothing is uploadable**, including under ``allow_all``: that
         grants authority over pages and says nothing about the filesystem.
    Any gate failing raises a ValidationError and nothing is sent.
    """
    logger.info(f"Tool called: upload_file (css_selector={css_selector!r}, "
                f"file_path={file_path!r}, id={id!r})")
    session = _store.route(id)
    # The rules are read once; the session runs every gate and the upload in one
    # driver-lock hold (see docs/design/gate-atomicity.md).
    # The file is named to the gate, not passed alongside it: the gate resolves
    # it once, and the path it admitted is the only one the session can reach.
    result = await session.upload_file(
        css_selector, id=id, gate=upload_file_gate(_access_rules_for(session), file_path))
    logger.info("Tool finished: upload_file")
    return result


@mcp.tool()
@_tool
async def press_key(css_selector: str, key: str, id: str) -> dict:
    """Focus an element on a tab and press a single control key — the press-key action.

    The keyboard counterpart of ``click``, for controls a coordinate click can't
    reach: elements the page made keyboard-operable (a ``tabindex`` list row, an
    ARIA widget) rather than a ``<button>``/``<a>``. Four server-side gates, all
    default-deny, must pass:
      1. The tab's host must be listed under the ``press-key`` section of the
         allowlist (and not on the denylist) — a section separate from ``click``
         and ``write-text``.
      2. ``css_selector`` must resolve to exactly one element that is a real,
         visible, non-decoy *focusable* control (natively focusable, or carrying
         ``tabindex``). A bare ``<div onclick>`` with no ``tabindex`` is refused —
         it isn't focusable. Integrity, not intent.
      3. ``key`` must be a control key (Enter/Space/Tab/Escape/arrows/Home/End/
         Page{Up,Down}) — never a character key; typing text is ``insert_text``'s
         job, gated separately by field label.
      4. Some page rule matching this URL must admit the control (its visible text
         matches the rule ``label``) AND list ``key`` in that rule's ``keys``.
    Any gate failing raises a ValidationError and nothing is pressed.
    """
    logger.info(f"Tool called: press_key (css_selector={css_selector!r}, key={key!r}, id={id!r})")
    session = _store.route(id)
    # The rules are read once; the session runs every gate and the key press in
    # one driver-lock hold (see docs/design/gate-atomicity.md).
    result = await session.press_key(css_selector, key, id=id,
                                     gate=press_key_gate(_access_rules_for(session), key))
    logger.info("Tool finished: press_key")
    return result


# Parameter annotations shared by the four DOM-query tools. FastMCP builds each
# tool's JSON schema from these annotations, so a `Field(description=...)` here is
# the only route by which the response caps reach an MCP client: without it the
# client sees four bare typed params and has to discover the caps by reading
# `dom/serialize.py` or by inferring them from a truncated response (#148).

IncludeHtml = Annotated[bool, Field(description=(
    "Also return the element's raw outer HTML in `html`. Off by default because HTML "
    "is large (an Amazon order card is ~355 chars of text but ~167 KB of HTML). This "
    "is the ONLY way to get more than 2000 chars out of one element: `text` is capped "
    "unconditionally and cannot be paginated."
))]

MaxHtmlBytes = Annotated[int, Field(description=(
    "Byte cap on the `html` field only. It does not affect `text`, and has no effect "
    "at all unless include_html=True. Truncation sets html_truncated=true — assert "
    "that it is false rather than trusting the returned length. Raise it (e.g. "
    "5000000) when you need a whole large node, such as a JSON blob rendered in <pre>."
))]

Limit = Annotated[int, Field(description=(
    "Paginate over matched ELEMENTS: return at most this many. Does not paginate the "
    "text or HTML within a single element."
))]

Offset = Annotated[int, Field(description=(
    "Paginate over matched ELEMENTS: skip this many before returning. Does not "
    "paginate the text or HTML within a single element."
))]


# -- frame navigation tools -------------------------------------------------
#
# The DOM-read tools observe only the *focused* document. These move a tab's
# frame focus so those tools can inspect an iframe's contents; the focus persists
# (browden replays it across the window-refocus every op performs) until moved
# back or reset by a navigate/reload.

@mcp.tool()
@_tool
async def switch_to_frame(css_selector: str, id: str) -> dict:
    """Switch a tab's focus INTO the iframe matched by css_selector (same-origin only).

    browden's DOM-read tools (query_selector, get_element_by_id, screenshot, …) see
    only the focused document, so an iframe's contents are invisible until you focus
    it here. After this succeeds those tools observe the frame; call
    ``switch_to_default_content`` (or ``switch_to_parent_frame``) to leave. A
    navigate/reload also resets the focus to the top document.

    Gates, all default-deny: the tab's top URL must be on the read allowlist; the
    iframe's declared ``src`` is checked *before* switching; and *after* switching
    the frame's actual ``document.URL`` must be read-allowed AND same-origin with the
    top page — cross-origin frames are refused. On any failure the driver is returned
    to the top document and nothing inside the frame is inspected.
    """
    logger.info(f"Tool called: switch_to_frame (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)

    # The tab's profile decides the rules; every gate below uses this one set.
    access_rules = _access_rules_for(session)

    # The focused document must itself be readable before we descend into a frame.
    here = await session.document_url(id=id)
    if here is None:
        return tab_gone_envelope(id)
    if not ensure_url_allowed(access_rules, here):
        raise ValidationError(f"URL not on the read allowlist: {here}")

    # Pre-switch gate: refuse to even enter a frame whose DECLARED src is disallowed
    # (defense-in-depth; src may be None for a srcdoc frame — then rely on the post gate).
    pre = await session.frame_src(css_selector, id=id)
    if "error" in pre:
        return pre
    src = pre.get("src")
    if src:
        validate_url(src, access_rules.read_policy)  # raises → never switch

    # Switch in, then gate the ACTUAL landed document (authoritative) + same-origin.
    entered = await session.enter_frame(css_selector, id=id)
    if "error" in entered:
        return entered
    try:
        validate_and_ensure_same_origin(entered["top_url"], entered["frame_url"], access_rules.read_policy)
    except ValidationError:
        await session.switch_to_default_content(id=id)  # back out; take no action inside
        raise
    logger.info("Tool finished: switch_to_frame")
    return entered


@mcp.tool()
@_tool
async def switch_to_parent_frame(id: str) -> dict:
    """Switch a tab's focus up one frame level, toward the top document.

    Moves de-escalating (toward the already-read-allowed top page), so it carries no
    URL gate of its own.
    """
    logger.info(f"Tool called: switch_to_parent_frame (id={id!r})")
    session = _store.route(id)
    result = await session.switch_to_parent_frame(id=id)
    logger.info("Tool finished: switch_to_parent_frame")
    return result


@mcp.tool()
@_tool
async def switch_to_default_content(id: str) -> dict:
    """Switch a tab's focus back to its top-level document, exiting all iframes."""
    logger.info(f"Tool called: switch_to_default_content (id={id!r})")
    session = _store.route(id)
    result = await session.switch_to_default_content(id=id)
    logger.info("Tool finished: switch_to_default_content")
    return result


# -- DOM-query tools --------------------------------------------------------
#
# These take a REQUIRED id (the id from new_blank_tab / navigate / list_tabs).
# Like navigate / select_tab / force_reload_tab, they never default to "the
# active tab": the active tab is shared state the human also controls, so an
# implicit default would silently act on whichever tab happens to be focused.
# A id that no longer names an open tab comes back as
# {"error": ..., "id": ...}.

@mcp.tool()
@_tool
async def get_element_by_id(element_id: str, id: str,
                            include_html: IncludeHtml = False,
                            max_html_bytes: MaxHtmlBytes = 4096) -> dict:
    """document.getElementById on a tab — one element node, or found=false (not an error) if absent.

    Caps: `text` is truncated at 2000 chars (`text_length` / `text_truncated` report
    the real size) and cannot be paginated; attribute values at 256 chars
    (`attributes_truncated`). For the full content of a node, pass include_html=True
    with a max_html_bytes large enough to hold it. See docs/dom-reads.md.
    """
    logger.info(f"Tool called: get_element_by_id (element_id={element_id!r}, id={id!r})")
    session = _store.route(id)
    # H2: the session gates the tab's live URL in the same driver hold as the read.
    gate = read_gate(_access_rules_for(session))
    result = await session.get_element_by_id(
        element_id, id=id, gate=gate, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_element_by_id")
    return result


@mcp.tool()
@_tool
async def get_elements_by_class_name(class_names: str, id: str,
                                     limit: Limit = 10, offset: Offset = 0,
                                     include_html: IncludeHtml = False,
                                     max_html_bytes: MaxHtmlBytes = 4096) -> dict:
    """document.getElementsByClassName on a tab — space-separated names, element must have ALL. Paginated.

    Caps: `text` is truncated at 2000 chars (`text_length` / `text_truncated` report
    the real size) and cannot be paginated; attribute values at 256 chars
    (`attributes_truncated`). For the full content of a node, pass include_html=True
    with a max_html_bytes large enough to hold it. See docs/dom-reads.md.
    """
    logger.info(f"Tool called: get_elements_by_class_name (class_names={class_names!r}, id={id!r})")
    session = _store.route(id)
    # H2: the session gates the tab's live URL in the same driver hold as the read.
    gate = read_gate(_access_rules_for(session))
    result = await session.get_elements_by_class_name(
        class_names, id=id, gate=gate, limit=limit, offset=offset,
        include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: get_elements_by_class_name")
    return result


@mcp.tool()
@_tool
async def query_selector(css_selector: str, id: str,
                         include_html: IncludeHtml = False,
                         max_html_bytes: MaxHtmlBytes = 4096) -> dict:
    """document.querySelector on a tab — one element node, or found=false if no match. Invalid CSS → error.

    Caps: `text` is truncated at 2000 chars (`text_length` / `text_truncated` report
    the real size) and cannot be paginated; attribute values at 256 chars
    (`attributes_truncated`). For the full content of a node, pass include_html=True
    with a max_html_bytes large enough to hold it. See docs/dom-reads.md.
    """
    logger.info(f"Tool called: query_selector (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)
    # H2: the session gates the tab's live URL in the same driver hold as the read.
    gate = read_gate(_access_rules_for(session))
    result = await session.query_selector(
        css_selector, id=id, gate=gate, include_html=include_html, max_html_bytes=max_html_bytes)
    logger.info("Tool finished: query_selector")
    return result


@mcp.tool()
@_tool
async def query_selector_all(css_selector: str, id: str,
                             limit: Limit = 10, offset: Offset = 0,
                             include_html: IncludeHtml = False,
                             max_html_bytes: MaxHtmlBytes = 4096) -> dict:
    """document.querySelectorAll on a tab — paginated list of element nodes. Invalid CSS → error.

    Caps: `text` is truncated at 2000 chars (`text_length` / `text_truncated` report
    the real size) and cannot be paginated; attribute values at 256 chars
    (`attributes_truncated`). For the full content of a node, pass include_html=True
    with a max_html_bytes large enough to hold it. See docs/dom-reads.md.
    """
    logger.info(f"Tool called: query_selector_all (css_selector={css_selector!r}, id={id!r})")
    session = _store.route(id)
    # H2: the session gates the tab's live URL in the same driver hold as the read.
    gate = read_gate(_access_rules_for(session))
    result = await session.query_selector_all(
        css_selector, id=id, gate=gate, limit=limit, offset=offset,
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
    # H2: the session gates the tab's live URL in the same driver hold as the read.
    gate = read_gate(_access_rules_for(session))
    result = await session.screenshot(id=id, gate=gate)
    if isinstance(result, dict):  # tab gone — structured error, not an image
        return result
    logger.info("Tool finished: screenshot")
    return Image(data=result, format="png")


@mcp.tool()
@_tool
async def invalidate_dom_cache(id: str) -> dict:
    """Drop a tab's cached DOM snapshot — the next read re-fetches the live HTML.

    The DOM tools answer from a per-tab cached parse of the page, so a change the
    *page itself* made after that parse (its own JS revealing a panel, an
    infinite-scroll batch landing, a live region updating) is invisible to them.
    This throws that snapshot away and nothing else.

    The cheap counterpart to ``force_reload_tab``: no page load, so everything the
    page built up in the live DOM survives — a reload would discard it (and re-run
    every request the page makes). Reach for ``force_reload_tab`` only when you
    actually want the page re-fetched from the server.

    Not gated on the read allowlist: it drives no browser action and returns no
    page content — it only discards server-side state. Reads stay gated where they
    always were, on the tab's live URL at read time, so dropping a stale snapshot
    can't widen what an agent may read. Returns ``{"id": ..., "invalidated": true}``,
    or ``{"error": ..., "id": ...}`` if the tab is no longer open.
    """
    logger.info(f"Tool called: invalidate_dom_cache (id={id!r})")
    session = _store.route(id)
    result = await session.invalidate_dom_cache(id=id)
    logger.info("Tool finished: invalidate_dom_cache")
    return result


@mcp.tool()
@_tool
async def force_reload_tab(id: str) -> dict:
    """Reload the named tab and refresh its cached DOM."""
    logger.info(f"Tool called: force_reload_tab (id={id!r})")
    session = _store.route(id)
    # H2: the session gates the live URL before the reload and the landing after
    # it (a reload can 302 off-list too), all in one driver hold.
    result = await session.force_reload_tab(id=id, gate=read_gate(_access_rules_for(session)))
    logger.info("Tool finished: force_reload_tab")
    return result


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: resolve + load the allowlist, then serve.

    Replaces the module default with a file-watching refresher over the config
    the operator chose, so every tool (the internal consumers of
    ``_refresher.runtime_configuration``) gates against it — and picks up edits live, since
    ``_runtime_configuration_lifespan`` runs the refresher's poller while the server serves.
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
    # BROWDEN_RELOAD_INTERVAL shortens the hot-reload poll tick — the e2e suite
    # sets it so a config edit is picked up in fractions of a second instead of
    # the operator-friendly 10s default.
    interval = float(os.environ.get(
        "BROWDEN_RELOAD_INTERVAL", DEFAULT_RELOAD_INTERVAL_SECONDS))
    try:
        _refresher = RuntimeConfigurationRefresher.from_path(path, interval=interval)
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
