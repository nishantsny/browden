"""End-to-end: ``invalidate_dom_cache`` against a real (headless) Chrome.

Goes through ``BrowserSessionManager`` — the same path the MCP tool takes — so the
whole chain runs: live Selenium ``page_snapshot`` -> soup cache -> ``dom.query``.

The scenario the tool exists for is reproduced literally: the page mutates its own
DOM *behind the cache's back*. That's staged by calling the **backend** directly
(``_page_clicks_add``) instead of ``session.click`` — the session-level write
tools invalidate the cache for you, the page's own JS obviously does not. The page
is an inline ``data:`` document, so the run is deterministic and offline.

The other half of the contract is here too: a write through the session drops the
snapshot itself, so the read after it needs no invalidate — also when the write
raised after it had already changed the page.
"""
import importlib
import urllib.parse
from unittest.mock import patch

import pytest

from browden.configs.loader import RuntimeConfigurationRefresher
from browden.dependencies.selenium import StaleElementReferenceException
from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.mcp.validator import BrowdenRuntimeConfiguration, ValidationError
from gates import OPEN_GATE, OPEN_READ_GATE

HTML = """<html><body>
  <button id="add"
          onclick="var d = document.createElement('div');
                   d.id = 'late'; d.textContent = 'arrived';
                   document.body.appendChild(d);">Add</button>
  <input id="qty" value="5"
         onchange="document.getElementById('add').click(); this.replaceWith(this.cloneNode());">
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
    page = await session.navigate(DATA_URL, id=blank["id"], gate=OPEN_READ_GATE)
    primed = await session.query_selector("#marker", id=page["id"], gate=OPEN_READ_GATE)
    assert primed["found"] is True  # the cache now holds a parse of the page
    return page


def _page_clicks_add(backend):
    """Click ``#add`` on the focused tab straight through the backend, as the page's JS would."""
    backend.click_target(backend.target_snapshot("#add").ref)


@pytest.mark.asyncio
async def test_invalidate_makes_the_next_read_see_a_page_side_dom_change(session, backend):
    page = await _page_with_primed_cache(session)

    # The page mutates itself; browden's cached parse predates the change.
    _page_clicks_add(backend)

    stale = await session.query_selector("#late", id=page["id"], gate=OPEN_READ_GATE)
    assert stale["found"] is False  # served from the pre-mutation snapshot

    res = await session.invalidate_dom_cache(id=page["id"])
    assert res == {"id": page["id"], "invalidated": True}

    fresh = await session.query_selector("#late", id=page["id"], gate=OPEN_READ_GATE)
    assert fresh["found"] is True
    assert fresh["element"]["text"] == "arrived"
    assert fresh["reloaded"] is False  # re-fetched, not reloaded


@pytest.mark.asyncio
async def test_invalidate_keeps_live_dom_state_that_force_reload_discards(session, backend):
    # The distinction that makes this tool worth having: invalidate re-reads the
    # live DOM (JS-built state survives); force_reload re-fetches the page (it
    # doesn't). Same starting point, opposite outcomes.
    page = await _page_with_primed_cache(session)
    _page_clicks_add(backend)

    await session.invalidate_dom_cache(id=page["id"])
    assert (await session.query_selector("#late", id=page["id"], gate=OPEN_READ_GATE))["found"] is True

    reloaded = await session.force_reload_tab(id=page["id"], gate=OPEN_READ_GATE)
    assert reloaded["reloaded"] is True
    gone = await session.query_selector("#late", id=page["id"], gate=OPEN_READ_GATE)
    assert gone["found"] is False  # the reload threw the JS-built node away


@pytest.mark.asyncio
async def test_a_write_drops_the_snapshot_so_the_next_read_sees_what_it_changed(session):
    # The counterpart of the tests above: the same DOM change, made by browden's
    # own ``click`` this time, needs no invalidate_dom_cache.
    page = await _page_with_primed_cache(session)

    await session.click("#add", id=page["id"], gate=OPEN_GATE)

    fresh = await session.query_selector("#late", id=page["id"], gate=OPEN_READ_GATE)
    assert fresh["found"] is True
    assert fresh["reloaded"] is False  # re-fetched from the live DOM, not reloaded


@pytest.mark.asyncio
async def test_a_write_that_fails_after_acting_still_drops_the_snapshot(session):
    # ``#qty`` re-renders itself on change, the way a framework re-mounts a field:
    # clearing it fires the handler, which changes the page and swaps the input
    # out, so the typing that follows raises on a node that is gone. The page
    # changed all the same, so the snapshot from before must not answer the next read.
    page = await _page_with_primed_cache(session)

    with pytest.raises(StaleElementReferenceException):
        await session.insert_text("#qty", "2", id=page["id"], gate=OPEN_GATE)

    fresh = await session.query_selector("#late", id=page["id"], gate=OPEN_READ_GATE)
    assert fresh["found"] is True


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


@pytest.mark.asyncio
async def test_invalidate_drops_only_the_named_tabs_snapshot(session, backend):
    # The drop is keyed by the tab's own handle, so a sibling tab — same session,
    # same page, same kind of page-side mutation — must keep answering from its
    # own snapshot. Its *staleness* is what proves the invalidation was scoped.
    first = await _page_with_primed_cache(session)
    _page_clicks_add(backend)  # page 1 mutates itself
    second = await _page_with_primed_cache(session)
    _page_clicks_add(backend)  # page 2 mutates itself

    await session.invalidate_dom_cache(id=first["id"])

    assert (await session.query_selector("#late", id=first["id"], gate=OPEN_READ_GATE))["found"] is True
    assert (await session.query_selector("#late", id=second["id"], gate=OPEN_READ_GATE))["found"] is False

    # …and the sibling is exactly one invalidate away from catching up, so it was
    # holding a stale snapshot rather than having been broken by the first drop.
    await session.invalidate_dom_cache(id=second["id"])
    assert (await session.query_selector("#late", id=second["id"], gate=OPEN_READ_GATE))["found"] is True


@pytest.mark.asyncio
async def test_invalidate_is_not_read_gated_but_reads_still_are(session, tmp_path):
    """The tool skips the read gate; that must not widen what an agent can read.

    Its docstring justifies dropping the ``document_url`` + ``ensure_url_allowed``
    round-trip on the grounds that it drives no browser action and returns no page
    content. The claim only holds if reads stay gated on the tab's live URL — so
    this drives the real server tools against a tab parked on a URL the read policy
    refuses: the invalidate succeeds, the read that follows does not.

    A ``file://`` page keeps the run offline. The override keyed ``""`` is what
    re-enables the ``file://`` scheme at all (see ``validate_url``), while its path
    regex matches nothing on disk — so the refusal lands on the allowlist, which is
    the gate under test, rather than on the scheme check ahead of it.
    """
    import browden.mcp.server as server
    importlib.reload(server)

    page_file = tmp_path / "page.html"
    page_file.write_text(HTML)
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(f"file://{page_file}", id=blank["id"], gate=OPEN_READ_GATE)
    primed = await session.query_selector("#marker", id=page["id"], gate=OPEN_READ_GATE)
    assert primed["found"] is True  # readable while the policy still admits it

    refuses_this_page = BrowdenRuntimeConfiguration({
        "read": {"enabled": True, "tranco": {"enabled": False},
                 "website_overrides": {"": ["^/definitely-not-this-path/.*"]}},
    })
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(refuses_this_page)):
        assert await server.invalidate_dom_cache(id=page["id"]) == {
            "id": page["id"], "invalidated": True}

        with pytest.raises(ValidationError, match="not on the read allowlist"):
            await server.query_selector("#marker", page["id"])
