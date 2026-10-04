"""Every page-touching tool, raced against a concurrent ``navigate`` on its tab.

A gate only protects what it guards if nothing can move the tab between the
check and the read or write (docs/design/gate-atomicity.md). This module holds
the ONE classification of the tools that touch a page — ``TOOLS`` — and runs
each one through every race that applies to its kind:

* **READ** / **WRITE** — the tool is paused at each backend call it makes
  (``park_points``) while a ``navigate`` on the same tab is queued behind it,
  to a page that is readable but has no write rules (``OTHER``), and to an
  allowed URL the server redirects off-list (``BOUNCE`` → ``SECRET``).
* **LANDING** — the tool's own navigation/reload is redirected off-list and
  paused there while a ``list_tabs`` is queued behind it.
* **FRAME** — the tool moves a tab's frame focus into a document it must refuse
  (a cross-origin frame, an ancestor that moved cross-origin, or a top page that
  moved off-list) and is paused
  *mid-move* — switched, not yet checked — while a read is queued behind it.

After every race the same invariant must hold: no off-list content left the
browser, no action landed on a page without a write rule, and no content was
read from — and no focus left resting in — a frame that wasn't admitted. Adding a tool to
``TOOLS`` is all it takes to put it under every race of its kind.

Built on ``atomicity_harness``: a real ``BrowserSessionManager`` (real FIFO
driver lock) over a fake backend, with one backend call parked so each
interleaving is deterministic.
"""
import asyncio
import contextlib
import tempfile
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from atomicity_harness import TAB, OnePageBackend, make_session, serve, until_queued

from browden.mcp.validator import BrowdenRuntimeConfiguration, ValidationError

SHOP = "https://shop.example/item"        # readable, and every write action is allowed
OTHER = "https://other.example/item"      # readable, but no write rule at all
BOUNCE = "https://shop.example/go"        # readable — but the server 302s it to SECRET
SECRET = "https://secret.example/inbox"   # not readable
WIDGET = "https://shop.example/widget"    # a same-origin frame on SHOP: admitted
FOREIGN = "https://other.example/widget"  # readable, but cross-origin to SHOP: refused as a frame


def _page(text, button, field_label):
    return (f"<html><body><p id='x' class='c'>{text}</p>"
            f"<button id='go'>{button}</button>"
            f"<input id='u' type='file' aria-label='{field_label}'>"
            f"<input id='f' type='text' placeholder='{field_label}'></body></html>")


# A real file in a real allowed location: the upload gate judges the filesystem,
# so the race needs a path that passes it. The directory lives for the module.
_UPLOAD_DIR = tempfile.TemporaryDirectory()
_UPLOAD_FILE = str(Path(_UPLOAD_DIR.name) / "receipt.png")
Path(_UPLOAD_FILE).write_bytes(b"png")


# OTHER carries the very labels SHOP's write rules admit, so a write that is
# retargeted onto it would pass the label gate if the gate judged the wrong page.
PAGES = {
    SHOP: _page("shop", "Add to cart", "Grocery tip"),
    OTHER: _page("other", "Add to cart", "Grocery tip"),
    SECRET: _page("SECRET MAIL", "Delete account", "New password"),
    WIDGET: _page("widget", "Add to cart", "Grocery tip"),
    FOREIGN: _page("FOREIGN FRAME", "Add to cart", "Grocery tip"),
}

# SHOP's iframes. FOREIGN is readable on its own, so only the same-origin check
# refuses it as a frame — the check a split, check-outside-the-hold move skips.
FRAMES = {SHOP: {"#child": WIDGET, "#foreign": FOREIGN}}

_RULES = BrowdenRuntimeConfiguration({
    "read": {"tranco": {"enabled": False},
             "website_overrides": {"shop.example": [".*"], "other.example": [".*"]}},
    "click": {"shop.example": {"paths": [".*"], "label": r"(?i)add to cart"}},
    "write-text": {"shop.example": {"paths": [".*"], "label": r"(?i)grocery tip"}},
    "press-key": {"shop.example": [{"path": [".*"], "label": r"(?i)add to cart", "keys": ["Enter"]}]},
    "upload-file": {"shop.example": {"paths": [".*"], "label": r"(?i)grocery tip"}},
    "allowed_upload_locations": [_UPLOAD_DIR.name],
})

READ, WRITE, LANDING, FRAME = "read", "write", "landing", "frame"


@dataclass(frozen=True)
class Tool:
    kind: str
    call: Callable                      # server -> awaitable
    park_points: tuple[str, ...]        # backend calls the tool makes, to pause it at
    redirects: dict = field(default_factory=dict)  # extra redirects the tool's scenario needs
    setup: Callable | None = None       # backend -> None: the tab's state before the call


# THE classification. A new tool that reads page content, writes, navigates or
# reloads belongs here — then it is raced like everything else of its kind.
TOOLS = {
    "get_element_by_id": Tool(READ, lambda s: s.get_element_by_id("x", id=TAB),
                              ("document_url", "page_snapshot")),
    "get_elements_by_class_name": Tool(READ, lambda s: s.get_elements_by_class_name("c", id=TAB),
                                       ("document_url", "page_snapshot")),
    "query_selector": Tool(READ, lambda s: s.query_selector("#x", id=TAB),
                           ("document_url", "page_snapshot")),
    "query_selector_all": Tool(READ, lambda s: s.query_selector_all("#x", id=TAB),
                               ("document_url", "page_snapshot")),
    "screenshot": Tool(READ, lambda s: s.screenshot(id=TAB),
                       ("document_url", "screenshot")),
    "click": Tool(WRITE, lambda s: s.click("#go", id=TAB),
                  ("document_url", "target_snapshot")),
    "insert_text": Tool(WRITE, lambda s: s.insert_text("#f", "x", id=TAB),
                        ("document_url", "target_snapshot")),
    "press_key": Tool(WRITE, lambda s: s.press_key("#go", "Enter", id=TAB),
                      ("document_url", "target_snapshot")),
    "upload_file": Tool(WRITE, lambda s: s.upload_file("#u", _UPLOAD_FILE, id=TAB),
                        ("document_url", "target_snapshot")),
    "navigate": Tool(LANDING, lambda s: s.navigate(BOUNCE, id=TAB), ("navigate",)),
    "force_reload_tab": Tool(LANDING, lambda s: s.force_reload_tab(id=TAB), ("reload",),
                             redirects={SHOP: SECRET}),
    # FRAME: each call is one the gate must refuse, raced mid-move.
    "switch_to_frame": Tool(FRAME, lambda s: s.switch_to_frame("#foreign", id=TAB),
                            ("document_url", "enter_frame")),
    # Two levels deep, where the middle frame has since been navigated
    # cross-origin by the page (the backend can't model page JS, so it's set up).
    "switch_to_parent_frame": Tool(FRAME, lambda s: s.switch_to_parent_frame(id=TAB),
                                   ("switch_to_parent_frame",),
                                   setup=lambda b: b.path.extend(
                                       [("#mid", FOREIGN), ("#child", WIDGET)])),
    # Inside an admitted frame, but the top page has since moved off-list: the
    # return to the top is refused, and nothing of the top page may be read.
    "switch_to_default_content": Tool(FRAME, lambda s: s.switch_to_default_content(id=TAB),
                                      ("switch_to_default_content",),
                                      setup=lambda b: (b.path.append(("#child", WIDGET)),
                                                       setattr(b, "url", SECRET))),
}


def _backend(url=SHOP, redirects=None):
    return OnePageBackend(url, dict(PAGES), redirects={BOUNCE: SECRET, **(redirects or {})},
                          frames=FRAMES)


def _assert_nothing_escaped(backend):
    """The invariant every race must leave intact."""
    leaked = [r for r in backend.reads if r[0] == SECRET]
    assert leaked == [], f"off-list content left the browser: {leaked}"
    misplaced = [a for a in backend.actions if a[0] != SHOP]
    assert misplaced == [], f"a write landed on a page with no write rule: {misplaced}"
    foreign = [r for r in backend.reads if r[0] == FOREIGN]
    assert foreign == [], f"content was read from a frame that wasn't admitted: {foreign}"
    resting = [f for f in backend.path if f[1] == FOREIGN]
    assert resting == [], f"focus was left inside a frame that wasn't admitted: {backend.path}"


@pytest.fixture
def server():
    import importlib

    import browden.mcp.server as server
    importlib.reload(server)
    return server


def _of(*kinds):
    return [name for name, tool in TOOLS.items() if tool.kind in kinds]


@contextlib.contextmanager
def _serving(server, backend):
    """Route every id to a fresh session over ``backend``, gated by ``_RULES``; yield the session."""
    session = make_session(backend)
    with contextlib.ExitStack() as stack:
        for cm in serve(server, session, _RULES):
            stack.enter_context(cm)
        yield session


# -- controls: each tool decides the way its gate says, with nothing racing it --

@pytest.mark.parametrize("name", _of(READ, WRITE))
async def test_control_goes_through_on_the_allowed_page(server, name):
    backend = _backend(SHOP)
    with _serving(server, backend):
        result = await TOOLS[name].call(server)
    assert not (isinstance(result, dict) and "error" in result), result
    if TOOLS[name].kind == WRITE:
        assert [a[0] for a in backend.actions] == [SHOP]
    else:
        assert backend.reads


@pytest.mark.parametrize("name", _of(READ))
async def test_control_a_read_of_an_off_list_page_is_refused(server, name):
    backend = _backend(SECRET)
    with _serving(server, backend), pytest.raises(ValidationError, match="read allowlist"):
        await TOOLS[name].call(server)
    assert backend.reads == []


@pytest.mark.parametrize("name", _of(WRITE))
async def test_control_a_write_on_a_page_without_a_write_rule_is_refused(server, name):
    backend = _backend(OTHER)
    with _serving(server, backend), pytest.raises(ValidationError):
        await TOOLS[name].call(server)
    assert backend.actions == []


@pytest.mark.parametrize("name", _of(LANDING))
async def test_control_an_off_list_landing_is_bounced(server, name):
    backend = _backend(SHOP, TOOLS[name].redirects)
    with _serving(server, backend):
        result = await TOOLS[name].call(server)
    assert "left the allowlist" in result["error"] and result["url"] == SECRET
    assert backend.url == "about:blank"
    _assert_nothing_escaped(backend)


# -- the races ------------------------------------------------------------------

_RACES = [(name, park, target)
          for name in _of(READ, WRITE)
          for park in TOOLS[name].park_points
          for target in (OTHER, BOUNCE)]


@pytest.mark.parametrize("name,park,target", _RACES)
async def test_a_concurrent_navigate_cannot_retarget_a_read_or_write(server, name, park, target):
    # The tool starts on SHOP, where it is allowed, and is paused at ``park``. A
    # navigate on the same tab — to OTHER (readable, no write rule) or to BOUNCE
    # (allowed, but redirected off-list) — queues behind it for the driver lock.
    backend = _backend(SHOP)
    with _serving(server, backend) as session:
        backend.park_next(park)
        task = asyncio.create_task(TOOLS[name].call(server))
        assert await asyncio.to_thread(backend.parked.wait, 5), f"{name} never called {park}"
        nav = asyncio.create_task(server.navigate(target, id=TAB))
        await until_queued(session)
        backend.resume.set()
        await asyncio.gather(task, nav, return_exceptions=True)
    _assert_nothing_escaped(backend)


@pytest.mark.parametrize("name", _of(LANDING))
async def test_an_off_list_landing_is_bounced_before_any_other_request_runs(server, name):
    # The tool's own navigation/reload is redirected to SECRET and paused there.
    # Until the tab is bounced to about:blank it sits off-list; a list_tabs queued
    # in that window must not see it there (and, per H2, close the agent's tab).
    backend = _backend(SHOP, TOOLS[name].redirects)
    with _serving(server, backend) as session:
        backend.park_next(TOOLS[name].park_points[0])
        task = asyncio.create_task(TOOLS[name].call(server))
        assert await asyncio.to_thread(backend.parked.wait, 5)
        listing = asyncio.create_task(server.list_tabs())
        await until_queued(session)
        backend.resume.set()
        result, tabs = await asyncio.gather(task, listing)

    assert "left the allowlist" in result["error"]
    assert SECRET not in [t.get("url") for t in tabs]
    assert backend.closed == [], "list_tabs saw the tab off-list and closed it"
    assert backend.url == "about:blank"
    _assert_nothing_escaped(backend)


# -- frames: controls, then a read raced against a refused move ----------------

async def test_control_a_same_origin_frame_is_entered_and_read(server):
    backend = _backend(SHOP)
    with _serving(server, backend):
        await server.switch_to_frame("#child", id=TAB)
        await server.query_selector("#x", id=TAB)
    assert backend.path == [("#child", WIDGET)]
    assert backend.reads == [(WIDGET, "html")]


@pytest.mark.parametrize("name", _of(FRAME))
async def test_control_a_refused_frame_move_leaves_no_focus_inside(server, name):
    backend = _backend(SHOP)
    if TOOLS[name].setup:
        TOOLS[name].setup(backend)
    with _serving(server, backend), pytest.raises(ValidationError,
                                                  match="cross-origin|not on (the read )?allowlist"):
        await TOOLS[name].call(server)
    _assert_nothing_escaped(backend)


_FRAME_RACES = [(name, park) for name in _of(FRAME) for park in TOOLS[name].park_points]


@pytest.mark.parametrize("name,park", _FRAME_RACES)
async def test_no_read_sees_the_focus_inside_a_frame_mid_refusal(server, name, park):
    # The move is paused at ``park`` — for the switch itself, AFTER switching into
    # the frame and BEFORE it is checked. A read on the same tab queues behind it.
    # The check and the rollback must finish in that same hold, so the read can
    # only ever see an admitted document. A move split across holds (check here,
    # switch there, back out later) lets the queued read in between them.
    backend = _backend(SHOP)
    if TOOLS[name].setup:
        TOOLS[name].setup(backend)
    with _serving(server, backend) as session:
        backend.park_next(park)
        task = asyncio.create_task(TOOLS[name].call(server))
        assert await asyncio.to_thread(backend.parked.wait, 5), f"{name} never called {park}"
        read = asyncio.create_task(server.query_selector("#x", id=TAB))
        await until_queued(session)
        backend.resume.set()
        moved, _ = await asyncio.gather(task, read, return_exceptions=True)
    assert isinstance(moved, ValidationError), moved  # the move itself is refused
    _assert_nothing_escaped(backend)
