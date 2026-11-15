"""The ``software_webgl`` parameter from the wire inwards.

Two boundaries meet here. The tool takes ``0``/``1``/absent — a value an agent
types, so anything else must be refused with a message rather than guessed at —
and the session manager turns that into one backend mode switch, plus the
bookkeeping a relaunch forces: the tabs of the browser that was replaced are
gone, so their ids and cached DOM must go with them.
"""
import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.web_navigator.tab_id import format_tab_id

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web_navigator"))
from test_session import FakeBackend, LiveCountBackend          # noqa: E402

NAMESPACE = "ns"


def _session(backend):
    return BrowserSessionManager(backend, namespace=NAMESPACE, start_reaper=False)


@pytest.mark.asyncio
async def test_omitting_it_never_touches_the_mode(monkeypatch):
    backend = LiveCountBackend()
    session = _session(backend)

    result = await session.new_blank_tab(max_tabs=10)

    # Not even a call to ask: the running browser must be left exactly as it is.
    assert not any(c[0] == "set_software_webgl" for c in backend.calls if isinstance(c, tuple))
    assert result["software_webgl"] == 0
    assert result["relaunched"] is False


@pytest.mark.asyncio
async def test_asking_for_it_switches_the_mode_and_reports_the_relaunch():
    backend = LiveCountBackend()
    session = _session(backend)

    result = await session.new_blank_tab(max_tabs=10, software_webgl=True)

    assert ("set_software_webgl", True) in backend.calls
    assert backend.relaunches == [True]
    assert result["software_webgl"] == 1
    assert result["relaunched"] is True


@pytest.mark.asyncio
async def test_asking_twice_relaunches_once():
    backend = LiveCountBackend()
    session = _session(backend)

    first = await session.new_blank_tab(max_tabs=10, software_webgl=True)
    second = await session.new_blank_tab(max_tabs=10, software_webgl=True)

    # The second tab carries the same preference and pays nothing for it, which
    # is what lets a caller pass the mode it wants on every tab it opens.
    assert backend.relaunches == [True]
    assert first["relaunched"] is True
    assert second["relaunched"] is False
    assert second["software_webgl"] == 1


@pytest.mark.asyncio
async def test_the_switch_happens_before_the_tab_is_opened():
    backend = LiveCountBackend()
    session = _session(backend)

    await session.new_blank_tab(max_tabs=10, software_webgl=True)

    names = [c[0] if isinstance(c, tuple) else c for c in backend.calls]
    # Order matters: the tab must be opened in the browser the caller asked for,
    # not in the one about to be thrown away.
    assert names.index("set_software_webgl") < names.index("new_blank_tab")


@pytest.mark.asyncio
async def test_a_relaunch_drops_the_ids_of_the_browser_it_replaced():
    backend = FakeBackend()
    session = _session(backend)

    # A tab from before the switch, tracked the way any live tab is.
    session._registry.touch("h1")

    await session.new_blank_tab(max_tabs=10, software_webgl=True)

    # h1's window died with its browser; keeping the id would hand the agent one
    # whose every use ends in the tab-gone envelope.
    assert session._registry.tracked_ids() == ["h2"]


@pytest.mark.asyncio
async def test_no_relaunch_keeps_the_other_tabs():
    backend = FakeBackend()
    session = _session(backend)
    session._registry.touch("h1")

    await session.new_blank_tab(max_tabs=10, software_webgl=False)   # already off

    assert sorted(session._registry.tracked_ids()) == ["h1", "h2"]


@pytest.mark.asyncio
async def test_the_new_tabs_id_is_the_one_that_survives():
    backend = FakeBackend()
    session = _session(backend)
    session._registry.touch("h1")

    result = await session.new_blank_tab(max_tabs=10, software_webgl=True)

    assert result["id"] == format_tab_id(NAMESPACE, "h2")
    assert session._registry.tracked_ids() == ["h2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2, 20, 201, -1, 42])
async def test_the_tool_refuses_anything_but_0_or_1(value):
    # The parameter has exactly two meanings and no third; a 20 is a mistake,
    # and reading it as "on" would silently relaunch someone's browser.
    from browden.mcp import server

    result = await server.new_blank_tab(software_webgl=value)

    assert "error" in result
    assert "0 or 1" in result["error"]
    assert str(value) in result["error"]


@pytest.mark.asyncio
async def test_the_tool_passes_a_mode_not_a_number(monkeypatch):
    """0/1 is the wire's vocabulary; the manager is given a bool."""
    from browden.mcp import server

    seen = {}

    class FakeSession:
        async def new_blank_tab(self, max_tabs, incognito=False, software_webgl=None):
            seen["software_webgl"] = software_webgl
            return {"id": "ns-h1"}

    monkeypatch.setattr(server._store, "get_or_create_session", lambda *a, **k: FakeSession())

    await server.new_blank_tab(software_webgl=1)
    assert seen["software_webgl"] is True

    await server.new_blank_tab(software_webgl=0)
    assert seen["software_webgl"] is False

    await server.new_blank_tab()
    assert seen["software_webgl"] is None
