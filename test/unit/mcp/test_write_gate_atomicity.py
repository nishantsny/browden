"""A write action is decided and performed as one unit.

The write tools used to gate the tab's URL, query the element, judge it, then
act — each step taking the session's driver lock separately. Anything that got
the lock in between (a concurrent ``navigate`` on the same tab) or ran on the
event loop in between (the config hot-reload) changed what the action landed
on, or what it was judged by, after it was judged. The session now runs the
whole gate sequence and the action in one driver-lock hold
(docs/design/write-gate-atomicity.md); these tests pin that.

They drive a real ``BrowserSessionManager`` (real FIFO driver lock) over a fake
backend, and park the write inside its HTML read so an interleaving is
deterministic rather than timing-dependent.
"""
import asyncio
import threading
from unittest.mock import patch

import pytest

from browden.common.tab import TabInfo
from browden.configs.loader import RuntimeConfigurationRefresher
from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.mcp.validator import BrowdenRuntimeConfiguration, ValidationError, click_gate

SHOP = "https://shop.example/item"
OTHER = "https://other.example/account"
_SHOP_HTML = ("<html><body><button id='go'>Add to cart</button>"
              "<input id='f' type='text' placeholder='Grocery tip'></body></html>")
_OTHER_HTML = ("<html><body><button id='go'>Delete account</button>"
               "<input id='f' type='text' placeholder='New password'></body></html>")
TAB = "ns-h1"

# Both hosts are readable (so navigate may go to either); only shop.example may
# be written to — each write action on its own labelled control.
_SHOP_ONLY = BrowdenRuntimeConfiguration({
    "read": {"website_overrides": {"*": [".*"]}},
    "click": {"shop.example": {"paths": [".*"], "label": r"(?i)add to cart"}},
    "write-text": {"shop.example": {"paths": [".*"], "label": r"(?i)grocery tip"}},
    "press-key": {"shop.example": [{"path": [".*"], "label": r"(?i)add to cart", "keys": ["Enter"]}]},
})

# (tool call, the action the backend records) for each write tool.
WRITES = {
    "click": (lambda server: server.click("#go", id=TAB), ("click", "#go")),
    "insert_text": (lambda server: server.insert_text("#f", "x", id=TAB), ("insert_text", "#f")),
    "press_key": (lambda server: server.press_key("#go", "Enter", id=TAB), ("press_key", "#go")),
}


class OnePageBackend:
    """A single tab (``h1``) whose document is ``pages[url]``.

    ``park_next_read`` makes the next ``get_tab_html`` signal ``parked`` and
    block until ``resume`` is set — holding the driver lock, as a slow real read
    would. ``actions`` records every write as ``(url it landed on, action, selector)``.
    """

    def __init__(self, url: str, pages: dict | None = None):
        self.url = url
        self.pages = {SHOP: _SHOP_HTML, OTHER: _OTHER_HTML} if pages is None else pages
        self.actions: list[tuple[str, str, str]] = []
        self.html_reads = 0
        self.parked = threading.Event()
        self.resume = threading.Event()
        self._park = False

    def park_next_read(self) -> None:
        self._park = True

    def get_profile_dir(self):
        return "/fake/profile"

    def select_tab(self, handle):
        assert handle == "h1"

    def document_url(self):
        return self.url

    def current_url(self):
        return self.url

    def get_tab_html(self):
        self.html_reads += 1
        if self._park:
            self._park = False
            self.parked.set()
            assert self.resume.wait(5), "test never resumed the parked read"
        return self.pages[self.url]

    def navigate(self, url):
        self.url = url
        return TabInfo(handle="h1", url=url, title="t", selected=True, profile_dir="/fake/profile")

    def _act(self, action, css_selector):
        self.actions.append((self.url, action, css_selector))
        return {"url": self.url, "title": "t"}

    def click_element(self, css_selector):
        return {"clicked": True, **self._act("click", css_selector)}

    def insert_text_element(self, css_selector, value):
        return {"inserted": True, "value": value, **self._act("insert_text", css_selector)}

    def press_key_element(self, css_selector, key):
        return {"pressed": key, **self._act("press_key", css_selector)}


def _session(backend):
    return BrowserSessionManager(backend, namespace="ns", start_reaper=False)


async def _until_queued(session, n=1):
    """Yield until ``n`` coroutines are waiting on the session's driver lock."""
    for _ in range(500):
        if len(session._lock._waiters or ()) >= n:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("request never queued on the driver lock")


@pytest.fixture
def server():
    import importlib

    import browden.mcp.server as server
    importlib.reload(server)
    return server


def _serve(server, session, configuration=_SHOP_ONLY):
    """Route every id to ``session`` and gate with ``configuration``."""
    return patch.multiple(server, _refresher=RuntimeConfigurationRefresher.static(configuration)), \
        patch.object(server._store, "route", return_value=session)


# -- controls: the harness decides the way the gates say it should ----------

@pytest.mark.parametrize("write", WRITES)
async def test_control_write_on_the_allowed_page_goes_through(server, write):
    call, action = WRITES[write]
    backend = OnePageBackend(SHOP)
    policy, route = _serve(server, _session(backend))
    with policy, route:
        result = await call(server)
    assert "error" not in result and result["id"] == TAB
    assert backend.actions == [(SHOP, *action)]


@pytest.mark.parametrize("write", WRITES)
async def test_control_write_on_the_other_page_is_refused(server, write):
    call, _ = WRITES[write]
    backend = OnePageBackend(OTHER)
    policy, route = _serve(server, _session(backend))
    with policy, route, pytest.raises(ValidationError):
        await call(server)
    assert backend.actions == []


# -- the races ----------------------------------------------------------------

@pytest.mark.parametrize("write", WRITES)
async def test_concurrent_navigate_cannot_retarget_an_authorized_write(server, write):
    # The write starts on SHOP, where it is allowed. While it is inside its HTML
    # read, a navigate on the same tab queues for the driver lock. The write must
    # not land on OTHER — a page with no write rule at all.
    call, _ = WRITES[write]
    backend = OnePageBackend(SHOP)
    s = _session(backend)
    policy, route = _serve(server, s)
    with policy, route:
        backend.park_next_read()
        task = asyncio.create_task(call(server))
        await asyncio.to_thread(backend.parked.wait, 5)
        nav = asyncio.create_task(server.navigate(OTHER, id=TAB))
        await _until_queued(s)
        backend.resume.set()
        await asyncio.gather(task, nav, return_exceptions=True)

    assert not [a for a in backend.actions if a[0] == OTHER], \
        f"{write} was authorized on {SHOP} but performed on {OTHER}"


async def test_hot_reload_mid_request_cannot_authorize_what_neither_config_allows(server):
    # old: shop.example clickable, label "Add to cart"     (element says "Delete account" -> refuse)
    # new: shop.example denylisted, label "Delete account" (denylisted            -> refuse)
    # A reload landing between gate 1 (old) and gate 3 (new) must not add up to "allow".
    new = BrowdenRuntimeConfiguration({
        "denylist": {"shop.example": [".*"]},
        "read": {"website_overrides": {"*": [".*"]}},
        "click": {"shop.example": {"paths": [".*"], "label": r"(?i)delete account"}},
    })
    backend = OnePageBackend(SHOP, pages={SHOP: _OTHER_HTML})
    policy, route = _serve(server, _session(backend))
    with policy, route:
        backend.park_next_read()
        click = asyncio.create_task(server.click("#go", id=TAB))
        await asyncio.to_thread(backend.parked.wait, 5)
        server._refresher = RuntimeConfigurationRefresher.static(new)  # the reload lands
        backend.resume.set()
        await asyncio.gather(click, return_exceptions=True)

    assert backend.actions == [], "a mixed old/new decision authorized the click"


# -- the single hold: what it reads, and what it judges ------------------------

async def test_a_denied_page_is_never_read():
    # Gate 1 runs on the URL alone, before the page's HTML is fetched.
    backend = OnePageBackend(OTHER)
    s = _session(backend)
    with pytest.raises(ValidationError, match="not allowed on this page"):
        await s.click("#go", id=TAB, gate=click_gate(_SHOP_ONLY.access_rules_for("/fake/profile")))
    assert backend.html_reads == 0
    assert backend.actions == []


async def test_the_gate_judges_the_live_page_not_the_cached_snapshot():
    # A read caches SHOP with "Add to cart"; then the page's own JS relabels the
    # button (no navigation, so the cache is still "fresh"). The click must be
    # judged on what the page shows now.
    backend = OnePageBackend(SHOP)
    s = _session(backend)
    cached = await s.query_selector("#go", id=TAB)
    assert cached["element"]["text"] == "Add to cart"
    backend.pages[SHOP] = _OTHER_HTML  # the page changes itself
    with pytest.raises(ValidationError, match="does not match any click label"):
        await s.click("#go", id=TAB, gate=click_gate(_SHOP_ONLY.access_rules_for("/fake/profile")))
    assert backend.actions == []


async def test_an_invalid_selector_is_an_error_envelope_and_nothing_is_done():
    backend = OnePageBackend(SHOP)
    s = _session(backend)
    result = await s.click("button[", id=TAB, gate=click_gate(_SHOP_ONLY.access_rules_for("/fake/profile")))
    assert result["id"] == TAB and "invalid CSS selector" in result["error"]
    assert backend.actions == []


async def test_a_gone_tab_is_the_tab_gone_envelope(server):
    from browden.web_navigator.interface import TabNotFoundError

    class GoneBackend(OnePageBackend):
        def select_tab(self, handle):
            raise TabNotFoundError(f"tab {handle!r} is not open")

    backend = GoneBackend(SHOP)
    policy, route = _serve(server, _session(backend))
    with policy, route:
        result = await server.click("#go", id=TAB)
    assert "error" in result and result["id"] == TAB
    assert backend.actions == []
