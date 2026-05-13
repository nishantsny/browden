import pytest

from browser_guard.common.page import PageInfo
from browser_guard.web_navigator.session import IDLE_TTL_SECONDS, PageSession

PAGE_HTML = """
<html><body>
  <div id="logo" class="brand"></div>
  <div class="order-card js-card"></div>
  <div class="order-card js-card"></div>
  <div class="order-card js-card other"></div>
</body></html>
"""


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeBackend:
    """Records calls; minimal behaviour for the coordinator's needs."""

    def __init__(self):
        self.active = "h1"
        self.source = PAGE_HTML
        self.calls = []
        self.closed = []
        self.close_raises = None  # set to an exception instance to simulate failure

    def list_pages(self):
        self.calls.append("list_pages")
        return [PageInfo(id="h1", url="u1", title="t1", selected=True),
                PageInfo(id="h2", url="u2", title="t2", selected=False)]

    def new_page(self, url=None):
        self.calls.append(("new_page", url))
        return PageInfo(id="h2", url=url or "about:blank", title="t", selected=True)

    def close_page(self, page_id):
        self.calls.append(("close_page", page_id))
        if self.close_raises is not None:
            raise self.close_raises
        self.closed.append(page_id)

    def select_page(self, page_id):
        self.calls.append(("select_page", page_id))

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return PageInfo(id="h1", url=url, title="t", selected=True)

    def current_page_id(self):
        self.calls.append("current_page_id")
        return self.active

    def get_page_source(self, page_id=None):
        self.calls.append(("get_page_source", page_id))
        return self.source

    def reload(self, page_id=None):
        self.calls.append(("reload", page_id))
        return PageInfo(id=page_id or self.active, url="reloaded-url", title="reloaded-title", selected=True)


def make_session(backend=None, clock=None):
    return PageSession(backend or FakeBackend(),
                       clock=clock or FakeClock(), start_reaper=False)


def test_no_reaper_task_when_disabled():
    s = make_session()
    assert s._reaper_task is None


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

    # prime a cache entry for h1
    await s.get_element_by_id("logo")
    assert "h1" in s._cache._entries

    await s.navigate("https://www.amazon.com/")
    assert "h1" not in s._cache._entries  # navigate busted it

    await s.get_element_by_id("logo", page_id="h1")  # re-prime
    assert "h1" in s._cache._entries
    await s.new_page("https://www.amazon.com/")
    # new_page invalidates the *new* tab's id (h2); prime + bust h2 to check
    await s.get_element_by_id("logo", page_id="h2")
    assert "h2" in s._cache._entries
    await s.close_page("h2")
    assert "h2" not in s._cache._entries
    assert "h2" not in s._registry._last_access  # close forgets it
    assert "h2" in [c[1] for c in backend.calls if isinstance(c, tuple) and c[0] == "close_page"]


@pytest.mark.asyncio
async def test_get_element_by_id_found_and_missing():
    s = make_session(FakeBackend())
    found = await s.get_element_by_id("logo")
    assert found["found"] is True
    assert found["element"]["id"] == "logo"
    assert found["element"]["classes"] == ["brand"]
    assert found["reloaded"] is False
    assert found["page_id"] == "h1"

    missing = await s.get_element_by_id("nope")
    assert missing["found"] is False
    assert missing["element"] is None


@pytest.mark.asyncio
async def test_query_selector_all_envelope_and_pagination():
    s = make_session(FakeBackend())
    env = await s.query_selector_all(".order-card.js-card", limit=2, offset=0)
    assert env["total_count"] == 3
    assert env["returned"] == 2
    assert env["limit"] == 2
    assert env["offset"] == 0
    assert env["next_offset"] == 2
    assert len(env["elements"]) == 2
    assert all("order-card" in e["classes"] for e in env["elements"])

    last = await s.query_selector_all(".order-card.js-card", limit=2, offset=2)
    assert last["returned"] == 1
    assert last["next_offset"] is None


@pytest.mark.asyncio
async def test_invalid_css_returns_error_dict():
    s = make_session(FakeBackend())
    err = await s.query_selector("div::::bad")
    assert "invalid CSS selector" in err["error"]
    assert err["page_id"] == "h1"

    err2 = await s.query_selector_all("??")
    assert "invalid CSS selector" in err2["error"]


@pytest.mark.asyncio
async def test_force_reload_page_reloads_and_reports():
    backend = FakeBackend()
    s = make_session(backend)
    out = await s.force_reload_page()
    assert out == {"page_id": "h1", "url": "reloaded-url", "title": "reloaded-title", "reloaded": True}
    assert ("reload", "h1") in backend.calls
    assert "h1" in s._registry._last_access


@pytest.mark.asyncio
async def test_query_default_page_id_resolves_active_tab():
    backend = FakeBackend()
    backend.active = "tabZ"
    s = make_session(backend)
    res = await s.get_element_by_id("logo")
    assert res["page_id"] == "tabZ"
    assert "current_page_id" in backend.calls


def test_sweep_idle_closes_invalidates_forgets_idle_pages():
    backend = FakeBackend()
    clock = FakeClock()
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


def test_sweep_idle_swallows_close_errors():
    backend = FakeBackend()
    backend.close_raises = ValueError("Cannot close the last tab")
    clock = FakeClock()
    s = make_session(backend, clock=clock)
    s._registry.touch("only")
    clock.t += IDLE_TTL_SECONDS + 1

    s.sweep_idle()  # must not raise

    assert ("close_page", "only") in backend.calls
    assert "only" not in s._registry._last_access  # still dropped from tracking


def test_sweep_idle_is_noop_while_driver_busy():
    backend = FakeBackend()
    clock = FakeClock()
    s = make_session(backend, clock=clock)
    s._registry.touch("old")
    clock.t += IDLE_TTL_SECONDS + 1
    s._driver_busy = True  # simulate an in-flight asyncio.to_thread driver op

    s.sweep_idle()

    assert backend.closed == []
    assert "old" in s._registry._last_access  # left for the next tick
