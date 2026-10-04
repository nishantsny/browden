"""Write-gate cases the race table doesn't cover.

The per-tool races (a concurrent ``navigate`` retargeting a write) live in
``test_gate_races.py``, driven by its ``TOOLS`` table. What stays here: a config
hot-reload landing mid-request (the race GHSA-4mgj-cwrw-795x reported), and what
the single driver-lock hold reads and judges
(docs/design/gate-atomicity.md).

Uses ``atomicity_harness``: a real ``BrowserSessionManager`` over a fake backend.
"""
import asyncio

import pytest
from atomicity_harness import PROFILE, TAB, OnePageBackend, serve
from atomicity_harness import make_session as _session

from safe_agent_browser.configs.loader import RuntimeConfigurationRefresher
from safe_agent_browser.mcp.validator import SafeAgentBrowserRuntimeConfiguration, ValidationError, click_gate, read_gate

SHOP = "https://shop.example/item"
OTHER = "https://other.example/account"
_SHOP_HTML = ("<html><body><button id='go'>Add to cart</button>"
              "<input id='f' type='text' placeholder='Grocery tip'></body></html>")
_OTHER_HTML = ("<html><body><button id='go'>Delete account</button>"
               "<input id='f' type='text' placeholder='New password'></body></html>")

# Both hosts are readable (so navigate may go to either); only shop.example may
# be written to — each write action on its own labelled control.
_SHOP_ONLY = SafeAgentBrowserRuntimeConfiguration({
    "read": {"website_overrides": {"*": [".*"]}},
    "click": {"shop.example": {"paths": [".*"], "label": r"(?i)add to cart"}},
    "write-text": {"shop.example": {"paths": [".*"], "label": r"(?i)grocery tip"}},
    "press-key": {"shop.example": [{"path": [".*"], "label": r"(?i)add to cart", "keys": ["Enter"]}]},
})


def _backend(url, pages=None):
    return OnePageBackend(url, {SHOP: _SHOP_HTML, OTHER: _OTHER_HTML} if pages is None else pages)


def _serve(server, session, configuration=_SHOP_ONLY):
    return serve(server, session, configuration)


@pytest.fixture
def server():
    import importlib

    import safe_agent_browser.mcp.server as server
    importlib.reload(server)
    return server


# -- a hot reload mid-request ----------------------------------------------------

async def test_hot_reload_mid_request_cannot_authorize_what_neither_config_allows(server):
    # old: shop.example clickable, label "Add to cart"     (element says "Delete account" -> refuse)
    # new: shop.example denylisted, label "Delete account" (denylisted            -> refuse)
    # A reload landing between gate 1 (old) and gate 3 (new) must not add up to "allow".
    new = SafeAgentBrowserRuntimeConfiguration({
        "denylist": {"shop.example": [".*"]},
        "read": {"website_overrides": {"*": [".*"]}},
        "click": {"shop.example": {"paths": [".*"], "label": r"(?i)delete account"}},
    })
    backend = _backend(SHOP, pages={SHOP: _OTHER_HTML})
    policy, route, _ = _serve(server, _session(backend))
    with policy, route:
        backend.park_next("target_snapshot")
        click = asyncio.create_task(server.click("#go", id=TAB))
        await asyncio.to_thread(backend.parked.wait, 5)
        server._refresher = RuntimeConfigurationRefresher.static(new)  # the reload lands
        backend.resume.set()
        await asyncio.gather(click, return_exceptions=True)

    assert backend.actions == [], "a mixed old/new decision authorized the click"


# -- the single hold: what it reads, and what it judges ------------------------

async def test_a_denied_page_is_never_read():
    # Gate 1 runs on the URL alone, before the page's HTML is fetched.
    backend = _backend(OTHER)
    s = _session(backend)
    with pytest.raises(ValidationError, match="not allowed on this page"):
        await s.click("#go", id=TAB, gate=click_gate(_SHOP_ONLY.access_rules_for(PROFILE)))
    assert backend.reads == []
    assert backend.actions == []


async def test_the_gate_judges_the_live_page_not_the_cached_snapshot():
    # A read caches SHOP with "Add to cart"; then the page's own JS relabels the
    # button (no navigation, so the cache is still "fresh"). The click must be
    # judged on what the page shows now.
    backend = _backend(SHOP)
    s = _session(backend)
    cached = await s.query_selector("#go", id=TAB, gate=read_gate(_SHOP_ONLY.access_rules_for(PROFILE)))
    assert cached["element"]["text"] == "Add to cart"
    backend.pages[SHOP] = _OTHER_HTML  # the page changes itself
    with pytest.raises(ValidationError, match="does not match any click label"):
        await s.click("#go", id=TAB, gate=click_gate(_SHOP_ONLY.access_rules_for(PROFILE)))
    assert backend.actions == []


async def test_an_invalid_selector_is_an_error_envelope_and_nothing_is_done():
    backend = _backend(SHOP)
    s = _session(backend)
    result = await s.click("button[", id=TAB, gate=click_gate(_SHOP_ONLY.access_rules_for(PROFILE)))
    assert result["id"] == TAB and "invalid CSS selector" in result["error"]
    assert backend.actions == []


async def test_a_gone_tab_is_the_tab_gone_envelope(server):
    from safe_agent_browser.web_navigator.interface import TabNotFoundError

    class GoneBackend(OnePageBackend):
        def select_tab(self, handle):
            raise TabNotFoundError(f"tab {handle!r} is not open")

    backend = GoneBackend(SHOP, {SHOP: _SHOP_HTML})
    policy, route, _ = _serve(server, _session(backend))
    with policy, route:
        result = await server.click("#go", id=TAB)
    assert "error" in result and result["id"] == TAB
    assert backend.actions == []
