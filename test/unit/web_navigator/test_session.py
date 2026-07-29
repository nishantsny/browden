import asyncio
import time

import pytest

from browden.common.tab import TabInfo
from browden.web_navigator.interface import TabNotFoundError
from browden.mcp.session_management.browser_session_manager import (
    DRIVER_LOCK_TIMEOUT_SECONDS,
    IDLE_TTL_SECONDS,
    BrowserSessionManager,
)
from browden.mcp.validator import SessionBusyError

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
        self.live: set[str] | None = {"h1", "h2"}  # ids list_handles reports; None -> it raises
        self.close_raises = None  # set to an exception instance to simulate failure
        self.running = True  # what is_running reports

    def _check(self, handle):
        if handle in self.missing:
            raise TabNotFoundError(f"tab {handle!r} is not open")

    def get_profile_dir(self):
        return self.profile_dir

    def is_running(self):
        self.calls.append("is_running")
        return self.running

    def shutdown(self):
        self.calls.append("shutdown")

    def list_tabs(self):
        self.calls.append("list_tabs")
        return [TabInfo(handle="h1", url="u1", title="t1", selected=True, profile_dir=self.profile_dir),
                TabInfo(handle="h2", url="u2", title="t2", selected=False, profile_dir=self.profile_dir)]

    def list_handles(self):
        self.calls.append("list_handles")
        if self.live is None:
            raise RuntimeError("list_handles unavailable")
        return list(self.live)

    def new_blank_tab(self):
        self.calls.append("new_blank_tab")
        return TabInfo(handle="h2", url="about:blank", title="t", selected=True, profile_dir=self.profile_dir)

    def close_tab(self, handle):
        self.calls.append(("close_tab", handle))
        if self.close_raises is not None:
            raise self.close_raises
        self._check(handle)
        self.closed.append(handle)

    def select_tab(self, handle):
        self.calls.append(("select_tab", handle))
        self._check(handle)
        self.active = handle  # focus: subsequent focus-free ops act on this tab

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return TabInfo(handle="h1", url=url, title="t", selected=True, profile_dir=self.profile_dir)

    # get_tab_html / reload / screenshot take no handle: they act on the
    # currently focused tab (self.active), which the caller select_tab's first.
    def get_tab_html(self):
        self.calls.append(("get_tab_html", self.active))
        return self.source

    def reload(self):
        self.calls.append(("reload", self.active))
        return TabInfo(handle=self.active, url="reloaded-url", title="reloaded-title", selected=True, profile_dir=self.profile_dir)

    def screenshot(self):
        self.calls.append(("screenshot", self.active))
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
    assert ("select_tab", "h1") in backend.calls  # focused first
    assert ("screenshot", "h1") in backend.calls   # ...then screenshot the focused tab
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
async def test_invalidate_dom_cache_drops_the_entry_without_driving_the_page():
    backend = FakeBackend()
    s = make_session(backend)
    s._cache._entries["h1"] = object()  # type: ignore[assignment]

    res = await s.invalidate_dom_cache(id="ns-h1")

    assert res == {"id": "ns-h1", "invalidated": True}
    assert "h1" not in s._cache._entries  # the snapshot is gone…
    assert "h1" in s._registry._last_access  # …but the tab is still tracked
    # Nothing was reloaded, re-fetched, or even focused — existence is checked
    # with list_handles, which doesn't move the focused window.
    assert not any(c in backend.calls for c in [("reload", "h1"), ("get_tab_html", "h1"), ("select_tab", "h1")])


@pytest.mark.asyncio
async def test_invalidate_dom_cache_is_idempotent_on_an_uncached_page():
    s = make_session(FakeBackend())
    assert "h2" not in s._cache._entries
    res = await s.invalidate_dom_cache(id="ns-h2")
    assert res == {"id": "ns-h2", "invalidated": True}


@pytest.mark.asyncio
async def test_invalidate_dom_cache_on_dead_page_returns_error_and_drops_it():
    backend = FakeBackend()
    backend.live = {"h1"}  # h9 is not among the open tabs
    s = make_session(backend)
    s._registry.touch("h9")
    s._cache._entries["h9"] = object()  # type: ignore[assignment]

    res = await s.invalidate_dom_cache(id="ns-h9")

    assert res == {"id": "ns-h9",
                   "error": "tab ns-h9 is no longer open — call list_tabs for current tabs"}
    assert "h9" not in s._cache._entries
    assert "h9" not in s._registry._last_access  # dropped from tracking


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
    """reload() succeeds but the subsequent get_tab_html() fails — the tab
    died between the two backend calls. The supplied handle must be dropped from
    cache + registry, and a structured error returned. Regression: an earlier
    implementation defaulted handle from current_handle() but caught the
    exception with the *original* (None) argument, leaving the live id leaked."""
    backend = FakeBackend()
    s = make_session(backend)
    s._registry.touch("h2")
    s._cache._entries["h2"] = object()  # type: ignore[assignment]

    # Make get_tab_html raise (post-select_tab focus, mid-reload), but leave reload working.
    def get_tab_html():
        backend.calls.append(("get_tab_html", backend.active))
        raise TabNotFoundError(f"tab {backend.active!r} disappeared mid-reload")
    backend.get_tab_html = get_tab_html

    res = await s.force_reload_tab(id="ns-h2")

    assert res == {"id": "ns-h2",
                   "error": "tab ns-h2 is no longer open — call list_tabs for current tabs"}
    assert ("reload", "h2") in backend.calls  # the partial succeeded
    assert ("get_tab_html", "h2") in backend.calls  # …and the second call failed
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
async def test_select_dead_page_returns_envelope_and_drops_it():
    # Like every other per-tab op, selecting a gone tab returns the standard
    # tab-gone envelope (with the composite id, never the raw backend handle).
    backend = FakeBackend()
    backend.missing.add("h4")
    s = make_session(backend)
    s._registry.touch("h4")
    result = await s.select_tab("ns-h4")
    assert result["id"] == "ns-h4"
    assert "no longer open" in result["error"]
    assert "h4" not in s._registry._last_access


# -- driver lock (concurrent requests on one session) ------------------------

class SlowReadBackend(FakeBackend):
    """Reads take real time, so an unsynchronized pair WOULD interleave.

    ``get_tab_html`` runs off the loop in ``asyncio.to_thread``; sleeping in it
    gives a second coroutine every chance to slip a ``select_tab`` in between
    this one's focus and its read — which is exactly the cross-talk the lock
    exists to prevent. Each read returns the focused handle's own marker, so a
    stolen focus shows up as one tab's read returning another tab's document.
    """

    def get_tab_html(self):
        time.sleep(0.05)
        self.calls.append(("get_tab_html", self.active))
        return f"<html><body><p id='who'>{self.active}</p></body></html>"


@pytest.mark.asyncio
async def test_concurrent_reads_on_one_session_do_not_steal_each_others_focus():
    backend = SlowReadBackend()
    s = make_session(backend)

    # Four distinct tabs, so every read is a cold-cache one that really drives
    # the browser (a second read of the same tab would answer from the soup cache
    # and never reach the driver).
    handles = ("h1", "h2", "h3", "h4")
    results = await asyncio.gather(*(s.query_selector("#who", id=f"ns-{h}") for h in handles))

    assert [r["element"]["text"] for r in results] == list(handles)
    # Every read is focus-then-act, never focus-focus-read-read.
    driver_calls = [c for c in backend.calls if c[0] in ("select_tab", "get_tab_html")]
    assert driver_calls == [call for h in handles
                            for call in (("select_tab", h), ("get_tab_html", h))]


@pytest.mark.asyncio
async def test_driver_lock_gives_up_after_the_timeout():
    s = make_session()
    await s._lock.acquire()  # a request that never finishes

    with pytest.raises(SessionBusyError) as excinfo:
        async with s._driver_lock(timeout=0.05):
            pytest.fail("must not get the lock while another request holds it")

    assert "busy" in str(excinfo.value)
    assert excinfo.value.timeout_seconds == 0.05
    assert s._lock.locked()  # the timed-out waiter left the lock with its owner


@pytest.mark.asyncio
async def test_a_timed_out_request_surfaces_as_an_error_envelope():
    # The tool layer turns SessionBusyError into the envelope agents get back.
    from browden.mcp.server import _tool

    @_tool
    async def fake_tool(id: str) -> dict:
        raise SessionBusyError(DRIVER_LOCK_TIMEOUT_SECONDS)

    envelope = await fake_tool(id="ns-h1")

    assert envelope["id"] == "ns-h1"
    assert "busy" in envelope["error"]


@pytest.mark.asyncio
async def test_separate_sessions_do_not_share_a_lock():
    # Per-session locking: a profile stuck on a long op must not stall another.
    busy, free = make_session(), make_session()
    await busy._lock.acquire()

    await asyncio.wait_for(free.list_tabs(), timeout=1.0)  # unaffected


# -- idle reaper ------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_do_not_sweep():
    # Cleanup is the reaper's job alone. A tool that swept would pay for a
    # list_handles round-trip on every single call to maybe close a tab that has
    # been idle for an hour — the reaper's tick catches it either way.
    backend = FakeBackend()
    s = make_session(backend)

    await s.list_tabs()
    await s.new_blank_tab(max_tabs=10)
    await s.select_tab("ns-h1")
    await s.query_selector("#logo", id="ns-h1")
    await s.close_tab("ns-h2")

    # new_blank_tab's own cap check is the only list_handles here (one call),
    # and nothing closed a tab that the test didn't ask to close.
    assert backend.calls.count("list_handles") == 1
    assert backend.closed == ["h2"]


@pytest.mark.asyncio
async def test_reaper_re_reads_its_interval_every_tick(monkeypatch):
    # The interval is a getter so an infra.reap_interval_seconds edit lands on
    # the next wake-up — the reaper must not cache the value it started with.
    intervals = [0.01, 0.02]
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    s = make_session(FakeBackend())
    s._reap_interval_seconds = lambda: intervals[min(len(slept), len(intervals) - 1)]
    with pytest.raises(asyncio.CancelledError):
        await s._reaper_loop()

    assert slept == [0.01, 0.02, 0.02]


@pytest.mark.asyncio
async def test_reaper_survives_a_failing_sweep(monkeypatch):
    # A sweep that raises must not kill the loop: the session would keep its
    # idle tabs for the rest of the process's life.
    calls = {"n": 0}

    async def fake_sleep(seconds):
        calls["n"] += 1
        if calls["n"] > 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    s = make_session(FakeBackend())
    s.sweep_idle = lambda: (_ for _ in ()).throw(RuntimeError("driver exploded"))
    with pytest.raises(asyncio.CancelledError):
        await s._reaper_loop()

    assert calls["n"] == 3  # kept ticking after the failures


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


@pytest.mark.asyncio
async def test_sweep_waits_for_the_driver_lock(fake_clock):
    # Cleanup drives the driver too, so it must queue behind an in-flight op
    # rather than reaching into the browser while another request holds it.
    backend = FakeBackend()
    backend.live = {"old"}  # still open in Chrome, just idle
    clock = fake_clock()
    s = make_session(backend, clock=clock)
    s._registry.touch("old")
    clock.t += IDLE_TTL_SECONDS + 1

    await s._lock.acquire()  # stand in for a request holding the driver
    sweep = asyncio.create_task(s._sweep_idle_locked())
    await asyncio.sleep(0)
    assert backend.closed == []
    assert "list_handles" not in backend.calls  # blocked: didn't even reconcile

    s._lock.release()
    await sweep

    assert backend.closed == ["old"]  # ran as soon as the driver was free


@pytest.mark.asyncio
async def test_sweep_is_skipped_when_the_session_stays_busy(fake_clock):
    # Best-effort: cleanup that can't get the lock in time gives up quietly
    # (the next tick retries) instead of raising into the caller's tool.
    backend = FakeBackend()
    clock = fake_clock()
    s = make_session(backend, clock=clock)
    s._registry.touch("old")
    clock.t += IDLE_TTL_SECONDS + 1

    await s._lock.acquire()  # never released: the session stays busy
    await s._sweep_idle_locked(timeout=0.05)  # must not raise

    assert backend.closed == []
    assert "list_handles" not in backend.calls
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
    backend.live = None  # list_handles raises
    s = make_session(backend)
    s._registry.touch("h1")

    s.sweep_idle()  # must not raise

    assert "h1" in s._registry._last_access  # reconcile skipped, entry untouched


# -- per-session tab cap (max_tabs) -----------------------------------------

class LiveCountBackend(FakeBackend):
    """FakeBackend whose new_blank_tab/close_tab actually move the live tab set,
    so the tab-cap check (which reads ``list_handles()``) sees a count that rises
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
        return TabInfo(handle=handle, url="about:blank", title="t",
                       selected=True, profile_dir=self.profile_dir)

    def close_tab(self, handle):
        super().close_tab(handle)
        self.live.discard(handle)


@pytest.mark.asyncio
async def test_new_blank_tab_raises_at_the_tab_cap():
    """At the cap with nothing idle, new_blank_tab raises instead of opening one."""
    backend = FakeBackend()
    backend.live = {"h1", "h2"}  # already at a cap of 2
    s = make_session(backend)
    s._registry.touch("h1")  # both tabs are in active use, so the sweep
    s._registry.touch("h2")  # below has nothing to reclaim

    with pytest.raises(RuntimeError, match="session limit of 2 tabs reached"):
        await s.new_blank_tab(max_tabs=2)

    assert "new_blank_tab" not in backend.calls  # never asked the backend to open one
    assert backend.closed == []  # and an in-use tab is never sacrificed for a new one


@pytest.mark.asyncio
async def test_hitting_the_cap_reclaims_idle_tabs_and_retries(fake_clock):
    """At the cap, an idle tab is swept and the request succeeds on the retry.

    This is what keeps the cap self-healing now that tool calls don't sweep:
    without it, a session whose tabs went idle stays wedged at its cap until the
    reaper's next tick (up to `infra.reap_interval_seconds` later).
    """
    backend = LiveCountBackend()
    clock = fake_clock()
    s = make_session(backend, clock=clock)
    s._registry.touch("h1")

    t2 = await s.new_blank_tab(max_tabs=2)  # fills the session to its cap
    clock.t += IDLE_TTL_SECONDS + 1  # ... and both tabs go idle

    t3 = await s.new_blank_tab(max_tabs=2)

    assert t3["id"] != t2["id"]  # a real new tab, not the old one echoed back
    assert set(backend.closed) == {"h1", "h2"}  # the idle pair was reclaimed
    assert backend.live == {"h3"}  # back under the cap, holding only the new tab


@pytest.mark.asyncio
async def test_the_cap_sweep_only_runs_when_the_session_is_full():
    """Below the cap, opening a tab must not pay for a sweep."""
    backend = LiveCountBackend()  # one live tab, cap of 10
    s = make_session(backend)

    await s.new_blank_tab(max_tabs=10)

    # One list_handles: the cap check itself. A sweep would add a second.
    assert backend.calls.count("list_handles") == 1


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
