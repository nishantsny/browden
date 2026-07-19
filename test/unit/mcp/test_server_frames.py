"""Tool-level tests for the frame-navigation write... er, focus actions.

The session is mocked (so no real Chrome), but the frame gates run for real:
``switch_to_frame`` gates the iframe's declared src before switching and the
landed ``document.URL`` (read-allowed + same-origin) after; ``switch_to_parent_frame``
and ``switch_to_default_content`` RE-gate the landed ancestor/top on every call —
because another process may have navigated it to an untrusted page while we were
deeper in the tree (the same reason the read tools re-check ``current_url`` every
call, not just on navigate).

Each mocked session method returns exactly what the corresponding backend op would,
so the gate sees realistic ``frame_url`` / ``top_url`` pairs.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browden.configs.loader import AllowlistRefresher
from browden.mcp.validator import ActionAllowlist, ValidationError

# Only app.example.com is readable...
_READ_APP = ActionAllowlist({"read": {"website_overrides": {"app.example.com": [".*"]}}})
# ...vs the whole https web readable (to isolate the same-origin check from the
# read-allowed check).
_READ_OPEN = ActionAllowlist({"read": {"website_overrides": {"*": [".*"]}}})

TOP = "https://app.example.com/page"


def _session(**overrides) -> MagicMock:
    """A mocked BrowserSessionManager. Defaults are the same-origin happy path;
    pass return values per method to shape a scenario."""
    s = MagicMock()
    s.current_url = AsyncMock(return_value=overrides.get("current_url", TOP))
    s.frame_src = AsyncMock(
        return_value=overrides.get("frame_src", {"src": None, "id": "t"}))
    s.enter_frame = AsyncMock(
        return_value=overrides.get("enter_frame",
                                   {"frame_url": "https://app.example.com/widget",
                                    "top_url": TOP, "id": "t"}))
    s.switch_to_parent_frame = AsyncMock(
        return_value=overrides.get("switch_to_parent_frame",
                                   {"frame_url": "https://app.example.com/parent",
                                    "top_url": TOP, "id": "t"}))
    s.switch_to_default_content = AsyncMock(
        return_value=overrides.get("switch_to_default_content",
                                   {"frame_url": TOP, "top_url": TOP, "id": "t"}))
    return s


def _server(allowlist):
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    return server


# -- switch_to_frame (entry gate: before + after) ----------------------------

@pytest.mark.asyncio
async def test_switch_to_frame_same_origin_allowed():
    server = _server(_READ_OPEN)
    session = _session()
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_READ_OPEN)):
        result = await server.switch_to_frame("#child", "t")
    assert result["frame_url"] == "https://app.example.com/widget"
    session.switch_to_default_content.assert_not_awaited()  # no retreat on success


@pytest.mark.asyncio
async def test_switch_to_frame_refuses_already_untrusted_src_before_switching():
    # Scenario: the iframe is ALREADY pointed at an untrusted site. Its declared
    # src fails the pre-switch gate, so we never enter it.
    server = _server(_READ_APP)
    session = _session(frame_src={"src": "https://evil.com/ad", "id": "t"})
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_READ_APP)):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.switch_to_frame("#child", "t")
    session.enter_frame.assert_not_awaited()  # refused BEFORE switching


@pytest.mark.asyncio
async def test_switch_to_frame_refuses_cross_origin_landed_document():
    # Scenario: src looked innocent / redirected, but the landed document is a
    # different origin. The after-gate refuses it and retreats to the top document.
    server = _server(_READ_OPEN)  # whole web readable — so ONLY same-origin can refuse
    session = _session(
        frame_src={"src": "https://app.example.com/redirector", "id": "t"},
        enter_frame={"frame_url": "https://ads.other.com/frame", "top_url": TOP, "id": "t"})
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_READ_OPEN)):
        with pytest.raises(ValidationError, match="cross-origin"):
            await server.switch_to_frame("#child", "t")
    session.switch_to_default_content.assert_awaited_once_with(id="t")  # retreated


# -- switch_to_parent_frame / switch_to_default_content (RE-gate on ascent) ----

@pytest.mark.asyncio
async def test_parent_frame_same_origin_allowed():
    server = _server(_READ_OPEN)
    session = _session()
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_READ_OPEN)):
        result = await server.switch_to_parent_frame("t")
    assert result["frame_url"] == "https://app.example.com/parent"
    session.switch_to_default_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_frame_refuses_when_ancestor_moved_cross_origin():
    # The key scenario: while we were deeper in the tree, another process navigated
    # the PARENT frame to an untrusted (cross-origin) page. Ascending must re-verify
    # the landed document and refuse — then retreat to the top.
    server = _server(_READ_OPEN)  # whole web readable, so only same-origin can refuse
    session = _session(
        switch_to_parent_frame={"frame_url": "https://evil.com/hijacked", "top_url": TOP, "id": "t"})
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_READ_OPEN)):
        with pytest.raises(ValidationError, match="cross-origin"):
            await server.switch_to_parent_frame("t")
    session.switch_to_default_content.assert_awaited_once_with(id="t")  # retreated to top


@pytest.mark.asyncio
async def test_default_content_refuses_when_top_moved_to_untrusted():
    # Another process moved the TOP page itself to an untrusted URL since we
    # descended. Returning to default content must re-check the top document and
    # refuse (there is nowhere safer to retreat — the read tools also refuse to read it).
    server = _server(_READ_APP)  # only app.example.com readable
    evil_top = "https://evil.com/landing"
    session = _session(
        switch_to_default_content={"frame_url": evil_top, "top_url": evil_top, "id": "t"})
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_READ_APP)):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.switch_to_default_content("t")


@pytest.mark.asyncio
async def test_default_content_allowed_when_top_still_trusted():
    server = _server(_READ_APP)
    session = _session()  # default: top is app.example.com (trusted)
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_READ_APP)):
        result = await server.switch_to_default_content("t")
    assert result["top_url"] == TOP
