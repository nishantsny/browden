"""Read-gate cases the race table doesn't cover.

The per-tool races (a concurrent ``navigate`` retargeting a read, an off-list
``navigate`` / ``force_reload_tab`` landing seen by a queued request) live in
``test_gate_races.py``, driven by its ``TOOLS`` table. What stays here needs no
concurrency: the soup cache's own TTL reload, which re-loads the page inside a
read and can be redirected off-list.

Uses ``atomicity_harness``: a real ``BrowserSessionManager`` over a fake backend.
"""

import pytest
from atomicity_harness import TAB, OnePageBackend, make_session, serve

from safe_agent_browser.mcp.validator import SafeAgentBrowserRuntimeConfiguration, ValidationError
from safe_agent_browser.web_navigator.soup_cache import TTL_SECONDS

SHOP = "https://shop.example/item"
SECRET = "https://secret.example/inbox"
PAGES = {
    SHOP: "<html><body><p id='x' class='c'>shop</p></body></html>",
    SECRET: "<html><body><p id='x' class='c'>SECRET MAIL</p></body></html>",
}

# Only shop.example may be read; Tranco is off so nothing else sneaks in.
_READ_SHOP_ONLY = SafeAgentBrowserRuntimeConfiguration({
    "read": {"tranco": {"enabled": False}, "website_overrides": {"shop.example": [".*"]}},
})


def _backend(url=SHOP):
    return OnePageBackend(url, dict(PAGES))


@pytest.fixture
def server():
    import importlib

    import safe_agent_browser.mcp.server as server
    importlib.reload(server)
    return server


def _secret_reads(backend):
    return [r for r in backend.reads if r[0] == SECRET]


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
    assert _secret_reads(backend) == [], "the landed page was fetched before its landing was gated"


