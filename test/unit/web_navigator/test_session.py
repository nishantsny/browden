import asyncio
import time

import pytest

from browden.common.tab import TabInfo
from browden.web_navigator.interface import TabNotFoundError
from browden.mcp.session_management.BrowserSessionManager import IDLE_TTL_SECONDS, BrowserSessionManager

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
    raises ``TabNotFoundError``, like the real backend does for a dead handle.
    """

    def __init__(self):
        self.active = "h1"
        self.profile_dir = "/fake/profile"  # what get_profile_dir() returns
        self.source = PAGE_HTML
        self.calls = []
        self.closed = []
        self.missing: set[str] = set()
        self.live: set[str] | None = {"h1", "h2"}  # ids list_tab_ids reports; None -> it raises
        self.close_raises = None  # set to an exception instance to simulate failure
        self.running = True  # what is_running reports

    def _check(self, tab_id):
        if tab_id in self.missing:
            raise TabNotFoundError(f"tab {tab_id!r} is not open")

    def get_profile_dir(self):
        return self.profile_dir

    def is_running(self):
        self.calls.append("is_running")
        return self.running

    def shutdown(self):
        self.calls.append("shutdown")

    def list_tabs(self):
        self.calls.append("list_tabs")
        return [TabInfo(per_session_id="h1", url="u1", title="t1", selected=True, profile_dir=self.profile_dir),
                TabInfo(per_session_id="h2", url="u2", title="t2", selected=False, profile_dir=self.profile_dir)]

    def list_tab_ids(self):
        self.calls.append("list_tab_ids")
        if self.live is None:
            raise RuntimeError("list_tab_ids unavailable")
        return list(self.live)

    def new_blank_tab(self):
        self.calls.append("new_blank_tab")
        return TabInfo(per_session_id="h2", url="about:blank", title="t", selected=True, profile_dir=self.profile_dir)

    def close_tab(self, tab_id):
        self.calls.append(("close_tab", tab_id))
        if self.close_raises is not None:
            raise self.close_raises
        self._check(tab_id)
        self.closed.append(tab_id)

    def select_tab(self, tab_id):
        self.calls.append(("select_tab", tab_id))
        self._check(tab_id)

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return TabInfo(per_session_id="h1", url=url, title="t", selected=True, profile_dir=self.profile_dir)

    def get_page_source(self, tab_id=None):
        self.calls.append(("get_page_source", tab_id))
        self._check(tab_id)
        return self.source

    def reload(self, tab_id=None):
        self.calls.append(("reload", tab_id))
        self._check(tab_id)
        return TabInfo(per_session_id=tab_id or self.active, url="reloaded-url", title="reloaded-title", selected=True, profile_dir=self.profile_dir)

    def screenshot(self, tab_id=None):
        self.calls.append(("screenshot", tab_id))
        return b"\x89PNG\r\n\x1a\nfakepng"


def make_session(backend=None, clock=None):
    """Tests that don't manipulate time can omit ``clock``; sweep_idle tests pass a
    ``fake_clock()`` instance so they can ``clock.t += seconds`` to advance."""
    return BrowserSessionManager(backend or FakeBackend(), namespace="ns",
                       clock=clock or time.monotonic, start_reaper=False)


def test_no_reaper_task_when_disabled():
    assert make_session()._reaper_task is None


def test_close_shuts_down_backend():
    backend = FakeBackend()
    s = make_session(backend)
    s.close()
    assert backend.calls == ["shutdown"]


@pytest.mark.asyncio
async def test_close_cancels_the_reaper():
    # A real reaper task needs a running loop to create; assert close cancels it
    # and drops the reference (so shutdown works without an active loop later).
    backend = FakeBackend()
    s = BrowserSessionManager(backend, namespace="ns", start_reaper=True)
    task = s._reaper_task
    assert task is not None
    s.close()
    assert s._reaper_task is None
    assert "shutdown" in backend.calls
    await asyncio.sleep(0)  # let the loop process the requested cancellation
    assert task.cancelled()


def test_close_is_safe_without_a_reaper():
    # Called at interpreter exit on a session built with start_reaper=False.
    backend = FakeBackend()
    s = make_session(backend)
    s.close()  # must not raise despite _reaper_task being None
    assert backend.calls == ["shutdown"]


def test_profile_dir_comes_from_backend():
    assert make_session().profile_dir == "/fake/profile"


@pytest.mark.asyncio
async def test_is_live_reflects_backend_without_driving():
    backend = FakeBackend()
    s = make_session(backend)
    assert await s.is_live() is True
    backend.running = False
    assert await s.is_live() is False
    # Only the probe ran — nothing that could (re)launch a browser.
    assert backend.calls == ["is_running", "is_running"]


@pytest.mark.asyncio
async def test_nav_tools_dispatch_and_touch_registry():
    backend = FakeBackend()
    s = make_session(backend)

    await s.list_tabs()
    assert "list_tabs" in backend.calls
    assert set(s._registry._last_access) == {"h1", "h2"}

    await s.select_tab("ns-h2")
    assert ("select_tab", "h2") in backend.calls


@pytest.mark.asyncio
async def test_navigate_new_page_close_page_invalidate_cache():
    backend = FakeBackend()
    s = make_session(backend)

    await s.get_element_by_id("logo", id="ns-h1")
    assert "h1" in s._cache._entries

    await s.navigate("https://www.amazon.com/", id="ns-h1")
    assert "h1" not in s._cache._entries  # navigate busted it

    await s.new_blank_tab(max_tabs=10)
    await s.get_element_by_id("logo", id="ns-h2")
    assert "h2" in s._cache._entries
    await s.close_tab("ns-h2")
    assert "h2" not in s._cache._entries
    assert "h2" not in s._registry._last_access
    assert ("close_tab", "h2") in backend.calls


@pytest.mark.asyncio
async def test_get_element_by_id_found_and_missing_element():
    s = make_session(FakeBackend())
    found = await s.get_element_by_id("logo", id="ns-h1")
    assert found["found"] is True
    assert found["element"]["id"] == "logo"
    assert found["element"]["classes"] == ["brand"]
    assert found["reloaded"] is False
    assert found["id"] == "ns-h1"

    missing = await s.get_element_by_id("nope", id="ns-h1")
    assert missing["found"] is False
    assert missing["element"] is None


@pytest.mark.asyncio
async def test_query_selector_all_envelope_and_pagination():
    s = make_session(FakeBackend())
    env = await s.query_selector_all(".order-card.js-card", id="ns-h1", limit=2, offset=0)
    assert env["total_count"] == 3
    assert env["returned"] == 2
    assert env["limit"] == 2
    assert env["offset"] == 0
    assert env["next_offset"] == 2
    assert len(env["elements"]) == 2
    assert all("order-card" in e["classes"] for e in env["elements"])

    last = await s.query_selector_all(".order-card.js-card", id="ns-h1", limit=2, offset=2)
    assert last["returned"] == 1
    assert last["next_offset"] is None


@pytest.mark.asyncio
async def test_invalid_css_returns_error_dict():
    s = make_session(FakeBackend())
    err = await s.query_selector("div::::bad", id="ns-h1")
    assert "invalid CSS selector" in err["error"]
    assert err["id"] == "ns-h1"

    err2 = await s.query_selector_all("??", id="ns-h1")
    assert "invalid CSS selector" in err2["error"]


@pytest.mark.asyncio
async def test_force_reload_page_reloads_and_reports():
    backend = FakeBackend()
    s = make_session(backend)
    out = await s.force_reload_tab(id="ns-h1")
    assert out == {"id": "ns-h1", "url": "reloaded-url", "title": "reloaded-title", "reloaded": True}
    assert ("reload", "h1") in backend.calls
    assert "h1" in s._registry._last_access

    out2 = await s.force_reload_tab(id="ns-h2")
    assert out2["id"] == "ns-h2"
    assert ("reload", "h2") in backend.calls


@pytest.mark.asyncio
async def test_screenshot_returns_png_bytes_and_touches_registry():
    backend = FakeBackend()
    s = make_session(backend)
    # Seed a cache entry so we can confirm screenshot leaves it untouched (read-only).
    sentinel = object()
    s._cache._entries["h1"] = sentinel  # type: ignore[assignment]

    png = await s.screenshot(id="ns-h1")

    assert png == b"\x89PNG\r\n\x1a\nfakepng"
    assert ("select_tab", "h1") in backend.calls
    assert ("screenshot", None) in backend.calls
    assert "h1" in s._registry._last_access
    assert s._cache._entries["h1"] is sentinel  # cache not invalidated


@pytest.mark.asyncio
async def test_screenshot_on_dead_page_returns_error_and_drops_it():
    backend = FakeBackend()
    backend.missing.add("h6")
    s = make_session(backend)
    s._registry.touch("h6")
    s._cache._entries["h6"] = object()  # type: ignore[assignment]

    res = await s.screenshot(id="ns-h6")

    assert res == {"id": "ns-h6",
                   "error": "tab ns-h6 is no longer open — call list_tabs for current tabs"}
    assert "h6" not in s._registry._last_access
    assert "h6" not in s._cache._entries


# -- dead-page handling -----------------------------------------------------

@pytest.mark.asyncio
async def test_dom_query_on_dead_page_returns_error_and_drops_it():
    backend = FakeBackend()
    s = make_session(backend)
    # a leftover registry entry for a tab that has since been closed; no fresh
    # cache entry, so get_soup hits the backend and discovers the dead handle
    s._registry.touch("h7")
    backend.missing.add("h7")

    res = await s.query_selector_all(".order-card.js-card", id="ns-h7")
    assert res == {"id": "ns-h7",
                   "error": "tab ns-h7 is no longer open — call list_tabs for current tabs"}
    assert "h7" not in s._cache._entries
    assert "h7" not in s._registry._last_access  # dropped from tracking


@pytest.mark.asyncio
async def test_force_reload_on_dead_page_returns_error():
    backend = FakeBackend()
    backend.missing.add("h9")
    s = make_session(backend)
    res = await s.force_reload_tab(id="ns-h9")
    assert res["id"] == "ns-h9"
    assert "no longer open" in res["error"]


@pytest.mark.asyncio
async def test_force_reload_partial_failure_drops_supplied_page_id():
    """reload(pid) succeeds but the subsequent get_page_source(pid) fails — the tab
    died between the two backend calls. The supplied tab_id must be dropped from
    cache + registry, and a structured error returned. Regression: an earlier
    implementation defaulted tab_id from current_tab_id() but caught the
    exception with the *original* (None) argument, leaving the live id leaked."""
    backend = FakeBackend()
    s = make_session(backend)
    s._registry.touch("h2")
    s._cache._entries["h2"] = object()  # type: ignore[assignment]

    # Make get_page_source raise for h2, but leave reload working.
    def get_page_source(tab_id=None):
        backend.calls.append(("get_page_source", tab_id))
        raise TabNotFoundError(f"tab {tab_id!r} disappeared mid-reload")
    backend.get_page_source = get_page_source

    res = await s.force_reload_tab(id="ns-h2")

    assert res == {"id": "ns-h2",
                   "error": "tab ns-h2 is no longer open — call list_tabs for current tabs"}
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
    res = await s.navigate("https://www.amazon.com/", id="ns-h5")
    assert res == {"id": "ns-h5",
                   "error": "tab ns-h5 is no longer open — call list_tabs for current tabs"}
    assert "h5" not in s._registry._last_access


@pytest.mark.asyncio
async def test_close_dead_page_is_a_noop_success():
    backend = FakeBackend()
    backend.missing.add("h3")
    s = make_session(backend)
    s._registry.touch("h3")
    s._cache._entries["h3"] = object()  # type: ignore[assignment]

    await s.close_tab("ns-h3")  # must not raise

    assert ("close_tab", "h3") in backend.calls
    assert "h3" not in s._cache._entries
    assert "h3" not in s._registry._last_access


@pytest.mark.asyncio
async def test_select_dead_page_raises_and_drops_it():
    backend = FakeBackend()
    backend.missing.add("h4")
    s = make_session(backend)
    s._registry.touch("h4")
    with pytest.raises(TabNotFoundError):
        await s.select_tab("ns-h4")
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

    assert ("close_tab", "only") in backend.calls
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
    assert "list_tab_ids" not in backend.calls  # didn't even reconcile
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
    backend.live = None  # list_tab_ids raises
    s = make_session(backend)
    s._registry.touch("h1")

    s.sweep_idle()  # must not raise

    assert "h1" in s._registry._last_access  # reconcile skipped, entry untouched


# -- per-session tab cap (max_tabs) -----------------------------------------

class LiveCountBackend(FakeBackend):
    """FakeBackend whose new_blank_tab/close_tab actually move the live tab set,
    so the tab-cap check (which reads ``list_tab_ids()``) sees a count that rises
    and falls exactly like the real backend's does."""

    def __init__(self):
        super().__init__()
        self.live = {"h1"}  # one tab already open
        self._next = 1

    def new_blank_tab(self):
        self.calls.append("new_blank_tab")
        self._next += 1
        handle = f"h{self._next}"
        self.live.add(handle)
        return TabInfo(per_session_id=handle, url="about:blank", title="t",
                       selected=True, profile_dir=self.profile_dir)

    def close_tab(self, tab_id):
        super().close_tab(tab_id)
        self.live.discard(tab_id)


@pytest.mark.asyncio
async def test_new_blank_tab_raises_at_the_tab_cap():
    """At the per-session tab cap, new_blank_tab raises instead of opening one."""
    backend = FakeBackend()
    backend.live = {"h1", "h2"}  # already at a cap of 2
    s = make_session(backend)

    with pytest.raises(RuntimeError, match="session limit of 2 tabs reached"):
        await s.new_blank_tab(max_tabs=2)

    assert "new_blank_tab" not in backend.calls  # never asked the backend to open one


@pytest.mark.asyncio
async def test_tab_cap_is_on_live_count_so_closing_frees_a_slot():
    """The cap is on the live tab count, not a monotonic total: filling to the
    cap raises, but closing a tab reopens a slot and the next open succeeds."""
    backend = LiveCountBackend()  # starts with one live tab, h1
    s = make_session(backend)

    # Below the cap of 2 -> opening succeeds (h1, then h2).
    t2 = await s.new_blank_tab(max_tabs=2)

    # At the cap -> the next open raises and opens nothing.
    with pytest.raises(RuntimeError, match="session limit of 2 tabs reached"):
        await s.new_blank_tab(max_tabs=2)

    # Close one tab: the live count drops back under the cap...
    await s.close_tab(t2["id"])
    assert t2["id"].split("-", 1)[1] not in backend.live

    # ...so opening is allowed again — no exception, and a fresh tab is returned.
    reopened = await s.new_blank_tab(max_tabs=2)
    assert reopened["id"] != t2["id"]
    assert reopened["id"].split("-", 1)[1] in backend.live
