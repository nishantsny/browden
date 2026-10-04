"""The session's frame methods: one hold, gated, and what they leave behind on failure.

Policy (which frames the real rules admit) is pinned in
test/unit/mcp/test_server_frames.py; the backend's enter-or-roll-back in
test/unit/web_navigator/selenium_chrome/test_backend_frames.py. These pin the
session's own contract around them: the gate runs inside the hold, a refused
ascent retreats to the top, a failure drops the cached snapshot, and nothing
sweeps idle tabs outside the driver lock.
"""
import pytest

from browden.web_navigator.interface import FrameFocusError
from browden.web_navigator.soup_cache import TTL_SECONDS

from browden.mcp.validator import FrameGate, ValidationError
from test_session import OPEN_READ_GATE, FakeBackend, make_session

TAB = "ns-h1"
TOP = "https://example.test/h1"
CHILD = "https://example.test/child"


def _gate(page=None, src=None, landed=None, seen=None):
    """A FrameGate whose checks record into ``seen`` and raise where told to."""
    seen = seen if seen is not None else []

    def check(name, refuse):
        def run(*args):
            seen.append((name, *args))
            if refuse:
                raise ValidationError(f"{name} refused")
        return run
    return FrameGate(check_page=check("page", page), check_src=check("src", src),
                     check_landed=check("landed", landed))


class FrameFakeBackend(FakeBackend):
    """``FakeBackend`` plus a frame model: ``path`` is the focused tab's frame path."""

    def __init__(self, landed_url=CHILD):
        super().__init__()
        self.path: list[str] = []
        self.landed_url = landed_url

    def enter_frame(self, css_selector, check_src, check_landed):
        self.calls.append(("enter_frame", css_selector))
        check_src(CHILD)
        check_landed(TOP, self.landed_url)  # (the real backend rolls back on a raise)
        self.path.append(css_selector)
        return {"frame_url": self.landed_url, "top_url": TOP}

    def switch_to_parent_frame(self):
        self.calls.append("switch_to_parent_frame")
        if self.path:
            self.path.pop()
        return {"frame_url": self.landed_url, "top_url": TOP}

    def switch_to_default_content(self):
        self.calls.append("switch_to_default_content")
        self.path = []
        return {"frame_url": self.landed_url, "top_url": TOP}

    def reload(self):
        self.path = []  # like the real one: a reload returns the tab to its top document
        return super().reload()

    def retreat_to_top(self):
        self.calls.append("retreat_to_top")
        self.path = []

    def in_frame(self):
        return bool(self.path)


async def _cache_snapshot(s):
    """Fill the soup cache for the tab, so a test can see whether it was dropped."""
    await s.get_element_by_id("logo", id=TAB, gate=OPEN_READ_GATE)
    assert s._cache._entries


@pytest.mark.asyncio
async def test_enter_frame_runs_every_check_in_order_inside_one_hold():
    backend = FrameFakeBackend()
    s = make_session(backend)
    seen = []
    result = await s.enter_frame("#child", id=TAB, gate=_gate(seen=seen))
    assert [x[0] for x in seen] == ["page", "src", "landed"]
    assert seen[0] == ("page", TOP)  # the focused document we descend from
    assert result["frame_url"] == CHILD and result["id"] == TAB
    assert backend.path == ["#child"]


@pytest.mark.asyncio
async def test_enter_frame_refused_on_the_current_page_never_touches_the_frame():
    backend = FrameFakeBackend()
    s = make_session(backend)
    with pytest.raises(ValidationError, match="page refused"):
        await s.enter_frame("#child", id=TAB, gate=_gate(page=True))
    assert ("enter_frame", "#child") not in backend.calls


@pytest.mark.asyncio
async def test_a_refused_enter_drops_the_cached_snapshot():
    backend = FrameFakeBackend(landed_url="https://other.test/x")
    s = make_session(backend)
    await _cache_snapshot(s)
    with pytest.raises(ValidationError, match="landed refused"):
        await s.enter_frame("#child", id=TAB, gate=_gate(landed=True))
    assert not s._cache._entries


@pytest.mark.asyncio
async def test_a_refused_ascent_retreats_to_the_top_and_drops_the_snapshot():
    backend = FrameFakeBackend()
    s = make_session(backend)
    await s.enter_frame("#mid", id=TAB, gate=_gate())
    await s.enter_frame("#child", id=TAB, gate=_gate())
    await _cache_snapshot(s)
    with pytest.raises(ValidationError, match="landed refused"):
        await s.switch_to_parent_frame(id=TAB, gate=_gate(landed=True))
    assert backend.calls[-1] == "retreat_to_top"
    assert backend.path == []
    assert not s._cache._entries


@pytest.mark.asyncio
async def test_an_admitted_ascent_moves_up_one_level_without_retreating():
    backend = FrameFakeBackend()
    s = make_session(backend)
    await s.enter_frame("#mid", id=TAB, gate=_gate())
    await s.enter_frame("#child", id=TAB, gate=_gate())
    await s.switch_to_parent_frame(id=TAB, gate=_gate())
    assert backend.path == ["#mid"]
    assert "retreat_to_top" not in backend.calls


@pytest.mark.asyncio
async def test_a_refused_default_content_is_already_at_the_top():
    backend = FrameFakeBackend(landed_url=TOP)
    s = make_session(backend)
    await s.enter_frame("#child", id=TAB, gate=_gate())
    await _cache_snapshot(s)
    with pytest.raises(ValidationError, match="landed refused"):
        await s.switch_to_default_content(id=TAB, gate=_gate(landed=True))
    assert backend.path == []
    assert not s._cache._entries


@pytest.mark.asyncio
async def test_frame_methods_never_sweep_idle_tabs():
    # sweep_idle closes tabs, which switches the driver's window. It must only run
    # under the driver lock (the reaper's _sweep_idle_locked); a tool that swept
    # outside it could move another request's driver mid-read or mid-click.
    s = make_session(FrameFakeBackend())
    swept = []
    s.sweep_idle = lambda *a, **k: swept.append(a)

    await s.enter_frame("#child", id=TAB, gate=_gate())
    await s.switch_to_parent_frame(id=TAB, gate=_gate())
    await s.switch_to_default_content(id=TAB, gate=_gate())

    assert swept == []


@pytest.mark.asyncio
async def test_a_stale_reload_inside_a_frame_refuses_the_read_instead_of_reading_the_top(fake_clock):
    # Finding 3: an expired snapshot reloads the page, which returns the tab to its
    # top document. The read used to hand back the TOP page's elements as if they
    # were the frame's. Now it refuses — before fetching the top page at all.
    clock = fake_clock()
    backend = FrameFakeBackend()
    s = make_session(backend, clock=clock)
    await s.enter_frame("#child", id=TAB, gate=_gate())
    await _cache_snapshot(s)  # the frame's snapshot
    clock.t += TTL_SECONDS + 1
    fetches_before = [c for c in backend.calls if c[0:1] == ("get_tab_html",)]

    with pytest.raises(FrameFocusError, match="switch_to_frame again"):
        await s.get_element_by_id("logo", id=TAB, gate=OPEN_READ_GATE)

    assert ("reload", "h1") in backend.calls
    assert [c for c in backend.calls if c[0:1] == ("get_tab_html",)] == fetches_before  # no fetch
    assert backend.path == []           # the reload did reset the focus...
    assert not s._cache._entries        # ...and the stale entry is gone,
    again = await s.get_element_by_id("logo", id=TAB, gate=OPEN_READ_GATE)
    assert again["reloaded"] is False   # so the next read fetches the top fresh, no second reload


@pytest.mark.asyncio
async def test_a_stale_reload_at_the_top_still_just_reloads(fake_clock):
    clock = fake_clock()
    backend = FrameFakeBackend()
    s = make_session(backend, clock=clock)
    await _cache_snapshot(s)
    clock.t += TTL_SECONDS + 1
    result = await s.get_element_by_id("logo", id=TAB, gate=OPEN_READ_GATE)
    assert result["reloaded"] is True
