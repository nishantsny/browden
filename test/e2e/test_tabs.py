"""End-to-end: the tab tools (navigate / select_tab / close_tab / list_tabs)
against a real (headless) Chrome.

Self-contained — every page is an inline ``data:`` document, so the suite is
deterministic, offline, and needs no allowlisted host. The conftest fixture
runs Chrome headless in a throwaway profile.
"""
import urllib.parse

import pytest

from browden.web_navigator.selenium_chrome import SeleniumChromeBackend


def _data_url(token: str) -> str:
    # The token is unreserved (letters/underscore), so it survives percent-
    # encoding verbatim and we can assert on it inside the live data: URL.
    html = f"<html><head><title>{token}</title></head><body>{token}</body></html>"
    return "data:text/html," + urllib.parse.quote(html)


URL_A = _data_url("PAGE_ALPHA")
URL_B = _data_url("PAGE_BETA")


@pytest.fixture
def backend(tmp_path):
    b = SeleniumChromeBackend(profile_dir=str(tmp_path / "profile"))
    yield b
    b._drv().quit()


def test_navigate_changes_active_tab_url(backend):
    backend.new_blank_tab()
    page = backend.navigate(URL_A)
    assert "PAGE_ALPHA" in backend.current_url()

    info = backend.navigate(URL_B)

    assert info.per_session_id == page.per_session_id  # navigate stays on the same (active) tab
    assert "PAGE_BETA" in info.url
    assert "PAGE_BETA" in backend.current_url()
    assert "PAGE_ALPHA" not in backend.current_url()


def test_select_page_switches_active_tab(backend):
    backend.new_blank_tab()
    a = backend.navigate(URL_A)
    backend.new_blank_tab()
    b = backend.navigate(URL_B)

    backend.select_tab(a.per_session_id)
    assert backend.current_tab_id() == a.per_session_id

    backend.select_tab(b.per_session_id)
    assert backend.current_tab_id() == b.per_session_id


def test_list_pages_marks_exactly_one_selected(backend):
    backend.new_blank_tab()
    a = backend.navigate(URL_A)
    backend.new_blank_tab()
    b = backend.navigate(URL_B)
    backend.select_tab(a.per_session_id)

    pages = backend.list_tabs()

    assert {a.per_session_id, b.per_session_id} <= {p.per_session_id for p in pages}
    selected = [p for p in pages if p.selected]
    assert len(selected) == 1
    assert selected[0].per_session_id == a.per_session_id


def test_close_page_removes_only_that_tab(backend):
    backend.new_blank_tab()
    a = backend.navigate(URL_A)
    backend.new_blank_tab()
    b = backend.navigate(URL_B)
    assert {a.per_session_id, b.per_session_id} <= {p.per_session_id for p in backend.list_tabs()}

    backend.close_tab(b.per_session_id)

    remaining = {p.per_session_id for p in backend.list_tabs()}
    assert b.per_session_id not in remaining
    assert a.per_session_id in remaining
