"""End-to-end: the reaper's timer really closes idle tabs in real Chrome.

The unit tests prove the pieces — the sweep picks the right tabs, and the loop
re-reads its interval each tick — but with the backend faked and
``asyncio.sleep`` patched out, so neither shows the thing an operator actually
depends on: leave a tab alone and, one configured interval later, a real Chrome
window is really gone.

That is what this drives: a live headless Chrome, a session whose reaper task is
really running, and the same ``reap_interval_seconds`` getter the server hands
the store — here returning a fraction of a second so a tick lands inside a test
instead of two hours later.

The clock is the one thing faked. Idleness is judged against
``IDLE_TTL_SECONDS`` (an hour), so the test advances the manager's injected clock
rather than waiting for one; the interval, the tick, the sweep, and the tab
closing are all real.
"""
import asyncio

import pytest

from browden.mcp.session_management.browser_session_manager import (
    IDLE_TTL_SECONDS,
    BrowserSessionManager,
)

# Short enough that several ticks land inside the test's patience below, long
# enough not to spin the loop needlessly while Chrome starts up.
REAP_INTERVAL = 0.2
PATIENCE_SECONDS = 15.0


class Clock:
    """A monotonic clock the test advances by hand (``clock.t += seconds``)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def make_manager(new_backend, tmp_path):
    """``make(interval, start_reaper=...) -> (manager, clock, backend)``; reapers cancelled after.

    A live reaper is a task that outlives the test body, so each one is cancelled
    on teardown; otherwise it keeps ticking against a browser the ``new_backend``
    fixture has already shut down.
    """
    made: list[BrowserSessionManager] = []
    counter = {"n": 0}

    def make(interval: float, start_reaper: bool = True):
        clock = Clock()
        counter["n"] += 1
        backend = new_backend(tmp_path / f"profile{counter['n']}")
        manager = BrowserSessionManager(
            backend, namespace="e2e", clock=clock, start_reaper=start_reaper,
            reap_interval_seconds=lambda: interval)
        made.append(manager)
        return manager, clock, backend

    yield make

    for manager in made:
        if manager._reaper_task is not None:
            manager._reaper_task.cancel()


async def _wait_until(predicate, message: str) -> None:
    """Poll ``predicate`` until true, or fail after PATIENCE_SECONDS.

    The reaper fires on its own schedule, so the test waits for an outcome rather
    than sleeping a guessed amount and hoping.
    """
    deadline = asyncio.get_running_loop().time() + PATIENCE_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out after {PATIENCE_SECONDS:g}s waiting for: {message}")


@pytest.mark.asyncio
async def test_reaper_tick_closes_the_idle_tab_and_spares_the_used_one(make_manager):
    manager, clock, backend = make_manager(REAP_INTERVAL)

    abandoned = await manager.new_blank_tab(max_tabs=10)
    kept = await manager.new_blank_tab(max_tabs=10)
    abandoned_handle = abandoned["id"].split("-", 1)[1]
    kept_handle = kept["id"].split("-", 1)[1]
    assert {abandoned_handle, kept_handle} <= set(backend.list_handles())

    # Both tabs age past the idle TTL ...
    clock.t += IDLE_TTL_SECONDS + 1
    # ... but one is used again right now, which re-stamps only that tab.
    # (`list_tabs` would touch *every* tab, which is exactly what must not
    # happen here — hence select_tab, and hence the raw list_handles polling
    # below rather than going through the manager.)
    await manager.select_tab(kept["id"])

    await _wait_until(lambda: abandoned_handle not in backend.list_handles(),
                      "the reaper to close the abandoned tab")

    # The tab that was used since the clock moved is still open, and the reaper
    # dropped only the closed one from tracking.
    assert kept_handle in backend.list_handles()
    assert abandoned_handle not in manager._registry.tracked_ids()
    assert kept_handle in manager._registry.tracked_ids()


@pytest.mark.asyncio
async def test_one_tick_reaps_exactly_the_tabs_past_the_ttl(make_manager):
    """Five tabs, five different last-use times, one tick: the cut is the TTL.

    The staggered case the single-tab tests can't show — that the sweep judges
    each tab on *its own* last use rather than reaping the lot once anything is
    stale. The five are placed either side of the TTL boundary, including both
    tabs exactly on it (reaped: ``idle_pages`` compares ``>= ttl``) and one
    second inside it (kept), so an off-by-one in either direction fails here.

    The reaper is started only once the tabs are arranged, so exactly one
    well-defined sweep time (``reap_at``) is in play — with a live reaper and a
    fast tick, a sweep could otherwise land mid-arrangement.
    """
    manager, clock, backend = make_manager(REAP_INTERVAL, start_reaper=False)

    tabs = {}
    for name in ("a", "b", "c", "d", "e"):
        tabs[name] = (await manager.new_blank_tab(max_tabs=10))["id"]

    reap_at = clock.t + 10_000
    # Last-use times, relative to the moment the sweep will run:
    last_used = {
        "a": reap_at - IDLE_TTL_SECONDS - 100,  # long past the TTL   -> reaped
        "b": reap_at - IDLE_TTL_SECONDS - 1,    # a second past it    -> reaped
        "c": reap_at - IDLE_TTL_SECONDS,        # exactly on it       -> reaped
        "d": reap_at - IDLE_TTL_SECONDS + 1,    # a second inside it  -> kept
        "e": reap_at - 10,                      # used 10s ago        -> kept
    }
    for name, used_at in last_used.items():
        clock.t = used_at
        await manager.select_tab(tabs[name])  # touches only this tab

    clock.t = reap_at
    manager.start_reaper()  # from here, the next tick sweeps at reap_at

    def handles() -> set[str]:
        return set(backend.list_handles())

    def handle_of(name: str) -> str:
        return tabs[name].split("-", 1)[1]

    stale = {handle_of(n) for n in ("a", "b", "c")}
    fresh = {handle_of(n) for n in ("d", "e")}

    await _wait_until(lambda: not (stale & handles()),
                      "the reaper to close every tab past the TTL")

    assert fresh <= handles()  # everything used inside the TTL is still open
    assert fresh == set(manager._registry.tracked_ids())  # and only those are tracked


@pytest.mark.asyncio
async def test_no_tick_no_reap(make_manager):
    """Nothing is closed before the interval elapses — the timer is what fires it.

    Guards against a reap that is really being triggered by something else (a
    tool call, session setup): with a long interval and an idle tab, the tab must
    still be open after several would-be ticks at the configured cadence.
    """
    manager, clock, backend = make_manager(3600.0)  # no tick lands in this test

    tab = await manager.new_blank_tab(max_tabs=10)
    handle = tab["id"].split("-", 1)[1]
    clock.t += IDLE_TTL_SECONDS + 1  # idle, and stays idle

    await asyncio.sleep(REAP_INTERVAL * 5)  # ample time had the cadence been short

    assert handle in backend.list_handles()
