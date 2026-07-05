"""End-to-end: the tab tools (navigate / select_page / close_page / list_pages)
against a real (headless) Chrome.

Self-contained — every page is an inline ``data:`` document, so the suite is
deterministic, offline, and needs no allowlisted host. The conftest fixture
runs Chrome headless in a throwaway profile.
"""
import urllib.parse

import pytest

from browser_guard.web_navigator.selenium_chrome import SeleniumChromeBackend


def _data_url(token: str) -> str:
    # The token is unreserved (letters/underscore), so it survives percent-
    # encoding verbatim and we can assert on it inside the live data: URL.
    html = f"<html><head><title>{token}</title></head><body>{token}</body></html>"
    return "data:text/html," + urllib.parse.quote(html)


URL_A = _data_url("PAGE_ALPHA")
URL_B = _data_url("PAGE_BETA")


@pytest.fixture
def backend():
    b = SeleniumChromeBackend(id_namespace="e2e")
    yield b
    b._drv().quit()


def test_navigate_changes_active_tab_url(backend):
    page = backend.new_page(URL_A)
    assert "PAGE_ALPHA" in backend.current_url()

    info = backend.navigate(URL_B)

    assert info.id == page.id  # navigate stays on the same (active) tab
    assert "PAGE_BETA" in info.url
    assert "PAGE_BETA" in backend.current_url()
    assert "PAGE_ALPHA" not in backend.current_url()


def test_select_page_switches_active_tab(backend):
    a = backend.new_page(URL_A)
    b = backend.new_page(URL_B)

    backend.select_page(a.id)
    assert backend.current_page_id() == a.id

    backend.select_page(b.id)
    assert backend.current_page_id() == b.id


def test_list_pages_marks_exactly_one_selected(backend):
    a = backend.new_page(URL_A)
    b = backend.new_page(URL_B)
    backend.select_page(a.id)

    pages = backend.list_pages()

    assert {a.id, b.id} <= {p.id for p in pages}
    selected = [p for p in pages if p.selected]
    assert len(selected) == 1
    assert selected[0].id == a.id


def test_close_page_removes_only_that_tab(backend):
    a = backend.new_page(URL_A)
    b = backend.new_page(URL_B)
    assert {a.id, b.id} <= {p.id for p in backend.list_pages()}

    backend.close_page(b.id)

    remaining = {p.id for p in backend.list_pages()}
    assert b.id not in remaining
    assert a.id in remaining
