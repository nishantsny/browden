"""End-to-end: hitting the per-session tab cap reclaims idle tabs, against real Chrome.

The unit tests cover this against a fake backend; what only a real browser can
show is that the reclaim actually survives Chrome's own rules — notably that
closing the tabs the sweep picked really does free capacity, and that the
last-window guard (Chrome quits if you close the only window, so ``close_tab``
refuses) doesn't leave the retry stuck at the cap.

Driven at the session-manager layer rather than through the MCP server, because
the reclaim only triggers once a tab has been untouched for ``IDLE_TTL_SECONDS``
(an hour). The manager takes an injectable ``clock``, so the test advances time
instead of waiting for it; a server subprocess offers no such seam.
"""
import pytest

from browden.mcp.session_management.browser_session_manager import (
    IDLE_TTL_SECONDS,
    BrowserSessionManager,
)


class Clock:
    """A monotonic clock the test advances by hand (``clock.t += seconds``)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def session(new_backend, tmp_path):
    """A manager over a real headless Chrome, with a hand-driven clock.

    ``start_reaper=False``: the periodic tick is not what's under test here, and
    leaving it off keeps the only sweep in play the one ``new_blank_tab`` fires.
    """
    clock = Clock()
    backend = new_backend(tmp_path / "profile")
    manager = BrowserSessionManager(backend, namespace="e2e", clock=clock, start_reaper=False)
    return manager, clock


# A freshly launched Chrome already owns one window, so a cap of 2 leaves room
# for exactly one agent-opened tab — enough to sit at the cap with one tab that
# the sweep is allowed to reclaim.
MAX_TABS = 2


@pytest.mark.asyncio
async def test_cap_reclaims_an_idle_tab_and_the_retry_opens_one(session):
    manager, clock = session
    first = await manager.new_blank_tab(max_tabs=MAX_TABS)  # now at the cap

    clock.t += IDLE_TTL_SECONDS + 1  # the agent leaves that tab alone for an hour

    second = await manager.new_blank_tab(max_tabs=MAX_TABS)

    assert second["id"] != first["id"]  # a genuinely new tab, not the old one back
    open_ids = {t["id"] for t in await manager.list_tabs()}
    assert second["id"] in open_ids
    assert first["id"] not in open_ids  # the idle tab was really closed in Chrome
    assert len(open_ids) <= MAX_TABS  # and the cap still holds


@pytest.mark.asyncio
async def test_cap_still_refuses_when_nothing_is_idle(session):
    manager, _clock = session  # clock never advanced -> the tab stays fresh
    first = await manager.new_blank_tab(max_tabs=MAX_TABS)

    with pytest.raises(RuntimeError, match=f"session limit of {MAX_TABS} tabs reached"):
        await manager.new_blank_tab(max_tabs=MAX_TABS)

    # The in-use tab was not sacrificed to serve the request that failed.
    assert first["id"] in {t["id"] for t in await manager.list_tabs()}
