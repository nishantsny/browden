"""End-to-end: the tab tools (navigate / select_tab / close_tab / list_tabs)
against a real (headless) Chrome.

Self-contained — every tab is an inline ``data:`` document, so the suite is
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
def backend(tmp_path):
    b = SeleniumChromeBackend(profile_dir=str(tmp_path / "profile"), id_namespace="e2e")
    yield b
    b._drv().quit()


def test_navigate_changes_active_tab_url(backend):
    tab = backend.new_blank_tab()
    backend.navigate(URL_A)
    assert "PAGE_ALPHA" in backend.current_url()

    info = backend.navigate(URL_B)

    assert info.id == tab.id  # navigate stays on the same (active) tab
    assert "PAGE_BETA" in info.url
    assert "PAGE_BETA" in backend.current_url()
    assert "PAGE_ALPHA" not in backend.current_url()


def test_select_page_switches_active_tab(backend):
    a = backend.new_blank_tab()
    backend.navigate(URL_A)
    b = backend.new_blank_tab()
    backend.navigate(URL_B)

    backend.select_tab(a.id)
    assert backend.current_page_id() == a.id

    backend.select_tab(b.id)
    assert backend.current_page_id() == b.id


def test_list_pages_marks_exactly_one_selected(backend):
    a = backend.new_blank_tab()
    backend.navigate(URL_A)
    b = backend.new_blank_tab()
    backend.navigate(URL_B)
    backend.select_tab(a.id)

    tabs = backend.list_tabs()

    assert {a.id, b.id} <= {p.id for p in tabs}
    selected = [p for p in tabs if p.selected]
    assert len(selected) == 1
    assert selected[0].id == a.id


def test_close_page_removes_only_that_tab(backend):
    a = backend.new_blank_tab()
    backend.navigate(URL_A)
    b = backend.new_blank_tab()
    backend.navigate(URL_B)
    assert {a.id, b.id} <= {p.id for p in backend.list_tabs()}

    backend.close_tab(b.id)

    remaining = {p.id for p in backend.list_tabs()}
    assert b.id not in remaining
    assert a.id in remaining
