"""A read is gated on the page it actually reads, in the same driver-lock hold.

The read gate admits a tab by its live URL. That only protects the read if
nothing can move the tab between the check and the read. These tests pin
three ways something could:

* a concurrent ``navigate`` to an allowed URL that redirects off-list, queued
  between the read's URL check and its HTML fetch or screenshot;
* the soup cache's own TTL reload, which re-loads the page inside a read and
  can be redirected off-list;
* the landing check that bounces an off-list ``navigate`` / ``force_reload_tab``
  landing to ``about:blank``: another request must not see the tab off-list in
  between.

They use ``atomicity_harness``: a real ``BrowserSessionManager`` (real FIFO
driver lock) over a fake backend, with one backend call parked so the
interleaving is deterministic.
"""
import asyncio

import pytest
from atomicity_harness import TAB, OnePageBackend, make_session, serve, until_queued

from browden.mcp.validator import BrowdenRuntimeConfiguration, ValidationError
from browden.web_navigator.soup_cache import TTL_SECONDS

SHOP = "https://shop.example/item"
BOUNCE = "https://shop.example/go"     # allowed, but the server 302s it to SECRET
SECRET = "https://secret.example/inbox"
PAGES = {
    SHOP: "<html><body><p id='x' class='c'>shop</p></body></html>",
    SECRET: "<html><body><p id='x' class='c'>SECRET MAIL</p></body></html>",
}

# Only shop.example may be read; Tranco is off so nothing else sneaks in.
_READ_SHOP_ONLY = BrowdenRuntimeConfiguration({
    "read": {"tranco": {"enabled": False}, "website_overrides": {"shop.example": [".*"]}},
})

READS = {
    "get_element_by_id": lambda server: server.get_element_by_id("x", id=TAB),
    "get_elements_by_class_name": lambda server: server.get_elements_by_class_name("c", id=TAB),
    "query_selector": lambda server: server.query_selector("#x", id=TAB),
    "query_selector_all": lambda server: server.query_selector_all("#x", id=TAB),
    "screenshot": lambda server: server.screenshot(id=TAB),
}


def _backend(url=SHOP):
    return OnePageBackend(url, dict(PAGES), redirects={BOUNCE: SECRET})


@pytest.fixture
def server():
    import importlib

    import browden.mcp.server as server
    importlib.reload(server)
    return server


def _secret_reads(backend):
    return [r for r in backend.reads if r[0] == SECRET]


# -- controls ------------------------------------------------------------------

@pytest.mark.parametrize("read", READS)
async def test_control_read_on_the_allowed_page_goes_through(server, read):
    backend = _backend(SHOP)
    policy, route, sessions = serve(server, make_session(backend), _READ_SHOP_ONLY)
    with policy, route, sessions:
        result = await READS[read](server)
    assert not (isinstance(result, dict) and "error" in result)
    assert backend.reads and _secret_reads(backend) == []


@pytest.mark.parametrize("read", READS)
async def test_control_read_on_an_off_list_page_is_refused(server, read):
    backend = _backend(SECRET)
    policy, route, sessions = serve(server, make_session(backend), _READ_SHOP_ONLY)
    with policy, route, sessions, pytest.raises(ValidationError, match="read allowlist"):
        await READS[read](server)
    assert backend.reads == []


async def test_control_navigate_to_a_redirect_off_list_is_bounced(server):
    backend = _backend(SHOP)
    policy, route, sessions = serve(server, make_session(backend), _READ_SHOP_ONLY)
    with policy, route, sessions:
        result = await server.navigate(BOUNCE, id=TAB)
    assert "left the allowlist" in result["error"] and result["url"] == SECRET
    assert backend.url == "about:blank"


# -- the races ----------------------------------------------------------------

@pytest.mark.parametrize("read", READS)
async def test_concurrent_redirecting_navigate_cannot_retarget_a_read(server, read):
    # The read starts on SHOP. While it is checking the tab's URL, the agent
    # queues a navigate to BOUNCE (allowed), which the server redirects to
    # SECRET. The read must not come back with SECRET's content.
    backend = _backend(SHOP)
    s = make_session(backend)
    policy, route, sessions = serve(server, s, _READ_SHOP_ONLY)
    with policy, route, sessions:
        backend.park_next("document_url")
        task = asyncio.create_task(READS[read](server))
        await asyncio.to_thread(backend.parked.wait, 5)
        nav = asyncio.create_task(server.navigate(BOUNCE, id=TAB))
        await until_queued(s)
        backend.resume.set()
        await asyncio.gather(task, nav, return_exceptions=True)

    assert _secret_reads(backend) == [], f"{read} read the off-list page"


async def test_a_ttl_reload_that_redirects_off_list_is_not_read(server):
    # A cached SHOP snapshot has expired, so the next read reloads the tab —
    # and the reload is redirected off-list (a session-expiry bounce to another
    # domain, say). The read must not return what it landed on.
    class Clock:
        t = 1000.0

        def __call__(self):
            return self.t

    clock = Clock()
    backend = _backend(SHOP)
    policy, route, sessions = serve(server, make_session(backend, clock=clock), _READ_SHOP_ONLY)
    with policy, route, sessions:
        first = await server.query_selector("#x", id=TAB)
        assert first["element"]["text"] == "shop"
        backend.redirects[SHOP] = SECRET  # the next load of SHOP lands on SECRET
        clock.t += TTL_SECONDS
        result = None
        with pytest.raises(ValidationError, match="read allowlist"):
            result = await server.query_selector("#x", id=TAB)

    assert result is None
    assert backend.url == "about:blank", "the tab was left parked off-list"


@pytest.mark.parametrize("landing", ["navigate", "force_reload_tab"])
async def test_an_off_list_landing_is_bounced_before_any_other_request_runs(server, landing):
    # navigate / force_reload_tab land on SECRET (a redirect). Until the tab is
    # bounced to about:blank it is sitting off-list; a list_tabs queued in that
    # window would see it — and, per H2, close the agent's tab out from under it.
    backend = _backend(SHOP)
    if landing == "force_reload_tab":
        backend.redirects[SHOP] = SECRET  # the reload is redirected
    s = make_session(backend)
    policy, route, sessions = serve(server, s, _READ_SHOP_ONLY)
    with policy, route, sessions:
        backend.park_next("navigate" if landing == "navigate" else "reload")
        call = (server.navigate(BOUNCE, id=TAB) if landing == "navigate"
                else server.force_reload_tab(id=TAB))
        task = asyncio.create_task(call)
        await asyncio.to_thread(backend.parked.wait, 5)
        listing = asyncio.create_task(server.list_tabs())
        await until_queued(s)
        backend.resume.set()
        result, tabs = await asyncio.gather(task, listing)

    assert "left the allowlist" in result["error"]
    assert SECRET not in [t.get("url") for t in tabs]
    assert backend.closed == [], "list_tabs saw the tab off-list and closed it"
    assert backend.url == "about:blank"
