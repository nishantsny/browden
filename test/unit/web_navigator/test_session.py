import time

import pytest

from browser_guard.common.page import PageInfo
from browser_guard.web_navigator.interface import PageNotFoundError
from browser_guard.web_navigator.session import IDLE_TTL_SECONDS, PageSession

PAGE_HTML = """
<html><body>
  <div id="logo" class="brand"></div>
  <div class="order-card js-card"></div>
  <div class="order-card js-card"></div>
  <div class="order-card js-card other"></div>
</body></html>
"""


class FakeBackend:
    """Records calls; minimal behaviour for the coordinator's needs.

    ``missing`` is a set of page ids that are "no longer open" — touching one
    raises ``PageNotFoundError``, like the real backend does for a dead handle.
    """

    def __init__(self):
        self.active = "h1"
        self.source = PAGE_HTML
        self.calls = []
        self.closed = []
        self.missing: set[str] = set()
        self.live: set[str] | None = {"h1", "h2"}  # ids list_page_ids reports; None -> it raises
        self.close_raises = None  # set to an exception instance to simulate failure

    def _check(self, page_id):
        if page_id in self.missing:
            raise PageNotFoundError(f"tab {page_id!r} is not open")

    def list_pages(self):
        self.calls.append("list_pages")
        return [PageInfo(id="h1", url="u1", title="t1", selected=True),
                PageInfo(id="h2", url="u2", title="t2", selected=False)]

    def list_page_ids(self):
        self.calls.append("list_page_ids")
        if self.live is None:
            raise RuntimeError("list_page_ids unavailable")
        return list(self.live)

    def new_page(self, url=None):
        self.calls.append(("new_page", url))
        return PageInfo(id="h2", url=url or "about:blank", title="t", selected=True)

    def close_page(self, page_id):
        self.calls.append(("close_page", page_id))
        if self.close_raises is not None:
            raise self.close_raises
        self._check(page_id)
        self.closed.append(page_id)

    def select_page(self, page_id):
        self.calls.append(("select_page", page_id))
        self._check(page_id)

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return PageInfo(id="h1", url=url, title="t", selected=True)

    def current_page_id(self):
        self.calls.append("current_page_id")
        if self.active in self.missing:
            raise PageNotFoundError("there is no active tab")
        return self.active

    def get_page_source(self, page_id=None):
        self.calls.append(("get_page_source", page_id))
        self._check(page_id)
        return self.source

    def reload(self, page_id=None):
        self.calls.append(("reload", page_id))
        self._check(page_id)
        return PageInfo(id=page_id or self.active, url="reloaded-url", title="reloaded-title", selected=True)


def make_session(backend=None, clock=None):
    """Tests that don't manipulate time can omit ``clock``; sweep_idle tests pass a
    ``fake_clock()`` instance so they can ``clock.t += seconds`` to advance."""
    return PageSession(backend or FakeBackend(),
                       clock=clock or time.monotonic, start_reaper=False)


def test_no_reaper_task_when_disabled():
    assert make_session()._reaper_task is None


@pytest.mark.asyncio
async def test_nav_tools_dispatch_and_touch_registry():
    backend = FakeBackend()
    s = make_session(backend)

    await s.list_pages()
    assert "list_pages" in backend.calls
    assert set(s._registry._last_access) == {"h1", "h2"}

    await s.select_page("h2")
    assert ("select_page", "h2") in backend.calls


@pytest.mark.asyncio
async def test_navigate_new_page_close_page_invalidate_cache():
    backend = FakeBackend()
    s = make_session(backend)

    await s.get_element_by_id("logo", page_id="h1")
    assert "h1" in s._cache._entries

    await s.navigate("https://www.amazon.com/", page_id="h1")
    assert "h1" not in s._cache._entries  # navigate busted it

    await s.new_page("https://www.amazon.com/")
    await s.get_element_by_id("logo", page_id="h2")
    assert "h2" in s._cache._entries
    await s.close_page("h2")
    assert "h2" not in s._cache._entries
    assert "h2" not in s._registry._last_access
    assert ("close_page", "h2") in backend.calls


@pytest.mark.asyncio
async def test_get_element_by_id_found_and_missing_element():
    s = make_session(FakeBackend())
    found = await s.get_element_by_id("logo", page_id="h1")
    assert found["found"] is True
    assert found["element"]["id"] == "logo"
    assert found["element"]["classes"] == ["brand"]
    assert found["reloaded"] is False
    assert found["page_id"] == "h1"

    missing = await s.get_element_by_id("nope", page_id="h1")
    assert missing["found"] is False
    assert missing["element"] is None


@pytest.mark.asyncio
async def test_query_selector_all_envelope_and_pagination():
    s = make_session(FakeBackend())
    env = await s.query_selector_all(".order-card.js-card", page_id="h1", limit=2, offset=0)
    assert env["total_count"] == 3
    assert env["returned"] == 2
    assert env["limit"] == 2
    assert env["offset"] == 0
    assert env["next_offset"] == 2
    assert len(env["elements"]) == 2
    assert all("order-card" in e["classes"] for e in env["elements"])

    last = await s.query_selector_all(".order-card.js-card", page_id="h1", limit=2, offset=2)
    assert last["returned"] == 1
    assert last["next_offset"] is None


@pytest.mark.asyncio
async def test_invalid_css_returns_error_dict():
    s = make_session(FakeBackend())
    err = await s.query_selector("div::::bad", page_id="h1")
    assert "invalid CSS selector" in err["error"]
    assert err["page_id"] == "h1"

    err2 = await s.query_selector_all("??", page_id="h1")
    assert "invalid CSS selector" in err2["error"]


@pytest.mark.asyncio
async def test_force_reload_page_reloads_and_reports():
    backend = FakeBackend()
    s = make_session(backend)
    out = await s.force_reload_page(page_id="h1")
    assert out == {"page_id": "h1", "url": "reloaded-url", "title": "reloaded-title", "reloaded": True}
    assert ("reload", "h1") in backend.calls
    assert "h1" in s._registry._last_access

    out2 = await s.force_reload_page(page_id="h2")
    assert out2["page_id"] == "h2"
    assert ("reload", "h2") in backend.calls


# -- dead-page handling -----------------------------------------------------

@pytest.mark.asyncio
async def test_dom_query_on_dead_page_returns_error_and_drops_it():
    backend = FakeBackend()
    s = make_session(backend)
    # a leftover registry entry for a tab that has since been closed; no fresh
    # cache entry, so get_soup hits the backend and discovers the dead handle
    s._registry.touch("h7")
    backend.missing.add("h7")

    res = await s.query_selector_all(".order-card.js-card", page_id="h7")
    assert res == {"page_id": "h7",
                   "error": "page h7 is no longer open — call list_pages for current tabs"}
    assert "h7" not in s._cache._entries
    assert "h7" not in s._registry._last_access  # dropped from tracking


@pytest.mark.asyncio
async def test_force_reload_on_dead_page_returns_error():
    backend = FakeBackend()
    backend.missing.add("h9")
    s = make_session(backend)
    res = await s.force_reload_page(page_id="h9")
    assert res["page_id"] == "h9"
    assert "no longer open" in res["error"]


@pytest.mark.asyncio
async def test_force_reload_partial_failure_drops_supplied_page_id():
    """reload(pid) succeeds but the subsequent get_page_source(pid) fails — the tab
    died between the two backend calls. The supplied page_id must be dropped from
    cache + registry, and a structured error returned. Regression: an earlier
    implementation defaulted page_id from current_page_id() but caught the
    exception with the *original* (None) argument, leaving the live id leaked."""
    backend = FakeBackend()
    s = make_session(backend)
    s._registry.touch("h2")
    s._cache._entries["h2"] = object()  # type: ignore[assignment]

    # Make get_page_source raise for h2, but leave reload working.
    def get_page_source(page_id=None):
        backend.calls.append(("get_page_source", page_id))
        raise PageNotFoundError(f"tab {page_id!r} disappeared mid-reload")
    backend.get_page_source = get_page_source

    res = await s.force_reload_page(page_id="h2")

    assert res == {"page_id": "h2",
                   "error": "page h2 is no longer open — call list_pages for current tabs"}
    assert ("reload", "h2") in backend.calls  # the partial succeeded
    assert ("get_page_source", "h2") in backend.calls  # …and the second call failed
    assert "h2" not in s._cache._entries  # old entry dropped
    assert "h2" not in s._registry._last_access  # tracking dropped


@pytest.mark.asyncio
async def test_navigate_on_dead_page_returns_error_and_drops_it():
    backend = FakeBackend()
    backend.missing.add("h5")
    s = make_session(backend)
    s._registry.touch("h5")
    res = await s.navigate("https://www.amazon.com/", page_id="h5")
    assert res == {"page_id": "h5",
                   "error": "page h5 is no longer open — call list_pages for current tabs"}
    assert "h5" not in s._registry._last_access


@pytest.mark.asyncio
async def test_close_dead_page_is_a_noop_success():
    backend = FakeBackend()
    backend.missing.add("h3")
    s = make_session(backend)
    s._registry.touch("h3")
    s._cache._entries["h3"] = object()  # type: ignore[assignment]

    await s.close_page("h3")  # must not raise

    assert ("close_page", "h3") in backend.calls
    assert "h3" not in s._cache._entries
    assert "h3" not in s._registry._last_access


@pytest.mark.asyncio
async def test_select_dead_page_raises_and_drops_it():
    backend = FakeBackend()
    backend.missing.add("h4")
    s = make_session(backend)
    s._registry.touch("h4")
    with pytest.raises(PageNotFoundError):
        await s.select_page("h4")
    assert "h4" not in s._registry._last_access


# -- idle reaper ------------------------------------------------------------

def test_sweep_idle_closes_invalidates_forgets_idle_pages(fake_clock):
    backend = FakeBackend()
    backend.live = {"old1", "old2", "fresh"}  # all still open in Chrome
    clock = fake_clock()
    s = make_session(backend, clock=clock)
    s._registry.touch("old1")
    s._registry.touch("old2")
    s._cache._entries["old1"] = object()  # type: ignore[assignment]
    clock.t += IDLE_TTL_SECONDS + 1
    s._registry.touch("fresh")

    s.sweep_idle()

    assert set(backend.closed) == {"old1", "old2"}
    assert "old1" not in s._cache._entries
    assert "old1" not in s._registry._last_access
    assert "old2" not in s._registry._last_access
    assert "fresh" in s._registry._last_access  # not idle


def test_sweep_idle_swallows_close_errors(fake_clock):
    backend = FakeBackend()
    backend.live = {"only"}
    backend.close_raises = ValueError("Cannot close the last tab")
    clock = fake_clock()
    s = make_session(backend, clock=clock)
    s._registry.touch("only")
    clock.t += IDLE_TTL_SECONDS + 1

    s.sweep_idle()  # must not raise

    assert ("close_page", "only") in backend.calls
    assert "only" not in s._registry._last_access  # still dropped from tracking


def test_sweep_idle_is_noop_while_driver_busy(fake_clock):
    backend = FakeBackend()
    clock = fake_clock()
    s = make_session(backend, clock=clock)
    s._registry.touch("old")
    clock.t += IDLE_TTL_SECONDS + 1
    s._driver_busy = True  # simulate an in-flight asyncio.to_thread driver op

    s.sweep_idle()

    assert backend.closed == []
    assert "list_page_ids" not in backend.calls  # didn't even reconcile
    assert "old" in s._registry._last_access  # left for the next tick


def test_sweep_idle_reconciles_against_live_tabs(fake_clock):
    backend = FakeBackend()
    backend.live = {"h1"}  # only h1 is still open; "ghost" was closed in Chrome
    clock = fake_clock()
    s = make_session(backend, clock=clock)
    s._registry.touch("h1")
    s._registry.touch("ghost")
    s._cache._entries["ghost"] = object()  # type: ignore[assignment]

    s.sweep_idle()  # nothing is idle (fresh clock) — only reconciliation acts

    assert "ghost" not in s._registry._last_access  # dropped: no longer a live tab
    assert "ghost" not in s._cache._entries
    assert "h1" in s._registry._last_access  # still open -> kept
    assert backend.closed == []  # reconciliation never closes anything


def test_sweep_idle_skips_reconcile_when_enumeration_fails():
    backend = FakeBackend()
    backend.live = None  # list_page_ids raises
    s = make_session(backend)
    s._registry.touch("h1")

    s.sweep_idle()  # must not raise

    assert "h1" in s._registry._last_access  # reconcile skipped, entry untouched
