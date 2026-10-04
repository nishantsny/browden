"""End-to-end: the backend's one-script snapshots, and acting on a snapshot's element.

``GatedPage`` gates the URL a snapshot reports and acts on the element ref it
returns (docs/design/gate-atomicity.md), so these pin what real Chrome hands
back: the URL and HTML of the same document, the live match count and tag, a
ref that acts on that exact element, and a refusal once that element has left
the page. The pages are inline ``data:`` documents, so the run is offline.
"""
import urllib.parse

import pytest

from browden.web_navigator.interface import InvalidSelectorError

HTML = """<html><body>
  <p class="row">one</p><p class="row">two</p><p class="row">three</p>
  <button id="go" onclick="document.getElementById('out').textContent = 'clicked'">Go</button>
  <div id="out">idle</div>
  <button id="hidden" style="display:none">Hidden</button>
</body></html>"""
DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def backend(new_backend, tmp_path):
    b = new_backend(tmp_path / "profile")
    tab = b.new_blank_tab()
    b.select_tab(tab.handle)
    b.navigate(DATA_URL)
    return b


def test_page_snapshot_returns_the_documents_url_and_html_together(backend):
    snap = backend.page_snapshot()
    assert snap.url == backend.document_url()
    assert snap.url.startswith("data:text/html")
    assert 'id="go"' in snap.html and "three" in snap.html


def test_target_snapshot_reports_the_full_live_count_and_the_first_tag(backend):
    snap = backend.target_snapshot("p.row")
    assert (snap.count, snap.tag) == (3, "p")
    assert snap.url == backend.document_url()
    assert "three" in snap.html


def test_target_snapshot_of_no_match_has_no_ref(backend):
    snap = backend.target_snapshot("#nope")
    assert (snap.count, snap.tag, snap.ref) == (0, None, None)


def test_a_selector_the_browser_rejects_raises_invalid_selector(backend):
    with pytest.raises(InvalidSelectorError, match="not a valid selector"):
        backend.target_snapshot("p[")


def test_click_target_clicks_the_snapshotted_element(backend):
    result = backend.click_target(backend.target_snapshot("#go").ref)
    assert result["clicked"] is True
    assert "clicked" in backend.page_snapshot().html


def test_a_hidden_target_is_refused(backend):
    with pytest.raises(ValueError, match="not visible"):
        backend.click_target(backend.target_snapshot("#hidden").ref)


def test_an_element_that_left_the_page_is_not_acted_on(backend):
    # The page replaces the element after the snapshot: its ref is stale, and
    # the click must not land on whatever now stands in its place.
    ref = backend.target_snapshot("#go").ref
    backend.navigate(DATA_URL)  # a fresh document, with a fresh #go
    with pytest.raises(ValueError, match="left the page"):
        backend.click_target(ref)
    assert "idle" in backend.page_snapshot().html
