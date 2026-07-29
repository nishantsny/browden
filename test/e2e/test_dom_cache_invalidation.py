"""End-to-end: ``invalidate_dom_cache`` against a real (headless) Chrome.

Goes through ``BrowserSessionManager`` — the same path the MCP tool takes — so the
whole chain runs: live Selenium ``get_tab_html`` -> soup cache -> ``dom.query``.

The scenario the tool exists for is reproduced literally: the page mutates its own
DOM *behind the cache's back*. That's staged by calling the **backend** directly
(``backend.click_element``) instead of ``session.click`` — the session-level write
tools invalidate the cache for you, the page's own JS obviously does not. The page
is an inline ``data:`` document, so the run is deterministic and offline.
"""
import urllib.parse

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager

HTML = """<html><body>
  <button id="add"
          onclick="var d = document.createElement('div');
                   d.id = 'late'; d.textContent = 'arrived';
                   document.body.appendChild(d);">Add</button>
  <div id="marker">start</div>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def backend(new_backend, tmp_path):
    return new_backend(tmp_path / "profile")


@pytest.fixture
def session(backend):
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


async def _page_with_primed_cache(session):
    """Open the fixture page and prime its soup cache with one query."""
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])
    primed = await session.query_selector("#marker", id=page["id"])
    assert primed["found"] is True  # the cache now holds a parse of the page
    return page


@pytest.mark.asyncio
async def test_invalidate_makes_the_next_read_see_a_page_side_dom_change(session, backend):
    page = await _page_with_primed_cache(session)

    # The page mutates itself; browden's cached parse predates the change.
    backend.click_element("#add")

    stale = await session.query_selector("#late", id=page["id"])
    assert stale["found"] is False  # served from the pre-mutation snapshot

    res = await session.invalidate_dom_cache(id=page["id"])
    assert res == {"id": page["id"], "invalidated": True}

    fresh = await session.query_selector("#late", id=page["id"])
    assert fresh["found"] is True
    assert fresh["element"]["text"] == "arrived"
    assert fresh["reloaded"] is False  # re-fetched, not reloaded


@pytest.mark.asyncio
async def test_invalidate_keeps_live_dom_state_that_force_reload_discards(session, backend):
    # The distinction that makes this tool worth having: invalidate re-reads the
    # live DOM (JS-built state survives); force_reload re-fetches the page (it
    # doesn't). Same starting point, opposite outcomes.
    page = await _page_with_primed_cache(session)
    backend.click_element("#add")

    await session.invalidate_dom_cache(id=page["id"])
    assert (await session.query_selector("#late", id=page["id"]))["found"] is True

    reloaded = await session.force_reload_tab(id=page["id"])
    assert reloaded["reloaded"] is True
    gone = await session.query_selector("#late", id=page["id"])
    assert gone["found"] is False  # the reload threw the JS-built node away


@pytest.mark.asyncio
async def test_invalidate_on_a_closed_tab_reports_tab_gone(session):
    page = await _page_with_primed_cache(session)
    # A second tab so the one under test isn't the last (the last can't be closed).
    await session.new_blank_tab(max_tabs=10)
    await session.close_tab(page["id"])

    res = await session.invalidate_dom_cache(id=page["id"])

    assert "error" in res
    assert res["id"] == page["id"]
    assert "invalidated" not in res
