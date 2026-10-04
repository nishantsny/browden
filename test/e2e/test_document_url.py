"""End-to-end: the backend's ``document_url`` reports the focused document's URL against real Chrome.

The read/write gates key off ``document_url`` (the focused ``document.URL``), read
inside the same driver hold as the read or write they guard, rather than
``current_url`` (the top-level address-bar URL). With no frame switching these are
identical — the point of this test — so the gates behave as they did before
``document_url``; the divergence (and the frame-aware gating it enables) arrives
with frame navigation. The page is an inline ``data:`` document, so the run is
offline.
"""
import urllib.parse

import pytest

from safe_agent_browser.web_navigator.interface import TabNotFoundError

DATA_URL = "data:text/html," + urllib.parse.quote(
    "<html><body><h1 id='h'>hi</h1></body></html>")


@pytest.fixture
def backend(new_backend, tmp_path):
    return new_backend(tmp_path / "profile")


def test_document_url_equals_top_url_when_not_in_a_frame(backend):
    tab = backend.new_blank_tab()
    backend.select_tab(tab.handle)
    landed = backend.navigate(DATA_URL)

    doc = backend.document_url()

    # landed.url is the tab's top-level URL (driver.current_url). With no frame
    # focus, the focused-document URL must match it exactly — the invariant that
    # makes gating on document_url a no-op until frame focus exists.
    assert doc == landed.url
    assert doc.startswith("data:text/html")


def test_a_closed_tab_cannot_be_focused_to_read_its_url(backend):
    # Two tabs so we can close one without hitting the last-tab guard.
    keep = backend.new_blank_tab()
    victim = backend.new_blank_tab()
    backend.select_tab(victim.handle)
    backend.navigate(DATA_URL)
    backend.close_tab(victim.handle)

    with pytest.raises(TabNotFoundError):
        backend.select_tab(victim.handle)
    # the surviving tab still resolves
    backend.select_tab(keep.handle)
    assert backend.document_url()
