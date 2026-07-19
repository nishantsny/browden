"""End-to-end: the frame-navigation tools against a real (headless) Chrome.

Drives ``BrowserSessionManager`` directly (the same path the MCP tools take, minus
the server-layer allowlist gates — those are unit-tested in
``test/unit/mcp/validator/test_read_gates.py``). Proves the core mechanic: browden's
DOM-read tools see only the *focused* document, and ``enter_frame`` /
``switch_to_default_content`` move that focus so the existing read tools can inspect
an iframe's contents — and that the focus survives the window-refocus every op
performs (via the backend's frame-path replay).

The page is an inline ``data:`` document whose iframe uses ``srcdoc`` (its document
inherits the parent's origin), so the run is deterministic and offline.
"""
import urllib.parse

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager

# An iframe (id="child") whose OWN document holds #in-frame; the top document holds
# #top-only. Neither id appears in the other document — that disjointness is what
# lets each assertion below prove which document the read tools are focused on.
INNER = "<div id='in-frame' class='inner'>INSIDE</div><p class='note'>frame text</p>"
HTML = f"""<html><body>
  <div id="top-only" class="top">TOP</div>
  <iframe id="child" srcdoc="{INNER}"></iframe>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


async def _open(session):
    blank = await session.new_blank_tab(max_tabs=10)
    return await session.navigate(DATA_URL, id=blank["id"])


@pytest.mark.asyncio
async def test_frame_contents_are_invisible_until_switched(session):
    page = await _open(session)
    tab = page["id"]

    # Top document: the iframe element is visible, but its CONTENTS are not.
    assert (await session.query_selector("#child", id=tab))["found"] is True
    assert (await session.query_selector("#in-frame", id=tab))["found"] is False

    # A srcdoc frame has no src to pre-gate.
    assert (await session.frame_src("#child", id=tab))["src"] is None

    # Switch INTO the frame — now the read tools observe the frame's document.
    await session.enter_frame("#child", id=tab)
    inframe = await session.query_selector("#in-frame", id=tab)
    assert inframe["found"] is True
    assert inframe["element"]["text"] == "INSIDE"
    # ...and the TOP document's node is no longer in view.
    assert (await session.query_selector("#top-only", id=tab))["found"] is False


@pytest.mark.asyncio
async def test_focus_survives_repeated_reads(session):
    # Every read re-focuses the window (switch_to.window), which resets Chrome's
    # frame context; the backend must replay the frame path each time or the second
    # read would silently fall back to the top document.
    page = await _open(session)
    tab = page["id"]
    await session.enter_frame("#child", id=tab)

    for _ in range(3):
        res = await session.query_selector(".inner", id=tab)
        assert res["found"] is True
        assert res["element"]["text"] == "INSIDE"


@pytest.mark.asyncio
async def test_default_content_returns_to_top(session):
    page = await _open(session)
    tab = page["id"]
    await session.enter_frame("#child", id=tab)
    assert (await session.query_selector("#in-frame", id=tab))["found"] is True

    await session.switch_to_default_content(id=tab)
    assert (await session.query_selector("#top-only", id=tab))["found"] is True
    assert (await session.query_selector("#in-frame", id=tab))["found"] is False


@pytest.mark.asyncio
async def test_parent_frame_steps_back_up_one_level(session):
    page = await _open(session)
    tab = page["id"]
    await session.enter_frame("#child", id=tab)

    await session.switch_to_parent_frame(id=tab)
    # Back at the top document (single level deep).
    assert (await session.query_selector("#top-only", id=tab))["found"] is True
    assert (await session.query_selector("#in-frame", id=tab))["found"] is False


@pytest.mark.asyncio
async def test_navigate_resets_frame_focus(session):
    page = await _open(session)
    tab = page["id"]
    await session.enter_frame("#child", id=tab)

    # A navigate loads a new top document; the recorded frame focus must be dropped
    # so reads see the top page again without an explicit switch_to_default_content.
    await session.navigate(DATA_URL, id=tab)
    assert (await session.query_selector("#top-only", id=tab))["found"] is True
    assert (await session.query_selector("#in-frame", id=tab))["found"] is False
