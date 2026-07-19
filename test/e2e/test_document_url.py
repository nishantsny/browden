"""End-to-end: ``document_url`` reports the focused document's URL against real Chrome.

The read/write gates now key off ``document_url`` (the focused ``document.URL``)
rather than ``current_url`` (the top-level address-bar URL). With no frame switching
these are identical — the point of this test — so the change is behavior-preserving
on its own; the divergence (and the frame-aware gating it enables) arrives with the
frame-navigation tools. The page is an inline ``data:`` document, so the run is offline.
"""
import urllib.parse

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager

DATA_URL = "data:text/html," + urllib.parse.quote(
    "<html><body><h1 id='h'>hi</h1></body></html>")


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


@pytest.mark.asyncio
async def test_document_url_equals_top_url_when_not_in_a_frame(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    doc = await session.document_url(id=page["id"])
    top = await session.current_url(id=page["id"])

    # At the top document the two accessors agree — this is the invariant that makes
    # switching the gates from current_url to document_url a no-op until frame focus
    # exists.
    assert doc == top
    assert doc.startswith("data:text/html")


@pytest.mark.asyncio
async def test_document_url_none_for_a_closed_tab(session):
    # Two tabs so we can close one without hitting the last-tab guard.
    keep = await session.new_blank_tab(max_tabs=10)
    victim = await session.new_blank_tab(max_tabs=10)
    await session.navigate(DATA_URL, id=victim["id"])
    await session.close_tab(id=victim["id"])

    assert await session.document_url(id=victim["id"]) is None
    # the surviving tab still resolves
    assert await session.document_url(id=keep["id"]) is not None
