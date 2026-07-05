"""End-to-end: the DOM-query + force_reload tools against a real (headless) Chrome.

These go through ``BrowserSessionManager`` — the same path the MCP tools take — so they
exercise the full chain: live Selenium ``get_page_source`` -> soup cache ->
``dom.query`` -> ``dom.serialize`` node. The tab is an inline ``data:``
document, so the run is deterministic and offline.
"""
import urllib.parse

import pytest

from browser_guard.web_navigator.selenium_chrome import SeleniumChromeBackend
from browser_guard.web_navigator.session import BrowserSessionManager

HTML = """<html><body>
  <div id="logo" class="brand mark">BG</div>
  <ul id="items">
    <li class="item js-item">alpha</li>
    <li class="item js-item">beta</li>
    <li class="item js-item special">gamma</li>
  </ul>
  <p class="note">hello world</p>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def session():
    backend = SeleniumChromeBackend()
    s = BrowserSessionManager(backend, start_reaper=False)
    yield s
    backend._drv().quit()


@pytest.mark.asyncio
async def test_get_element_by_id(session):
    tab = await session.new_blank_tab()
    await session.navigate(DATA_URL, tab_id=tab.id)

    res = await session.get_element_by_id("logo", tab_id=tab.id)

    assert res["found"] is True
    assert res["element"]["id"] == "logo"
    assert res["element"]["classes"] == ["brand", "mark"]
    assert res["element"]["text"] == "BG"

    missing = await session.get_element_by_id("nope", tab_id=tab.id)
    assert missing["found"] is False
    assert missing["element"] is None


@pytest.mark.asyncio
async def test_get_elements_by_class_name(session):
    tab = await session.new_blank_tab()
    await session.navigate(DATA_URL, tab_id=tab.id)

    res = await session.get_elements_by_class_name("item js-item", tab_id=tab.id)

    assert res["total_count"] == 3
    assert res["returned"] == 3
    assert all("item" in e["classes"] for e in res["elements"])
    assert {e["text"] for e in res["elements"]} == {"alpha", "beta", "gamma"}


@pytest.mark.asyncio
async def test_query_selector_single_match(session):
    tab = await session.new_blank_tab()
    await session.navigate(DATA_URL, tab_id=tab.id)

    res = await session.query_selector("p.note", tab_id=tab.id)

    assert res["found"] is True
    assert res["element"]["tag"] == "p"
    assert res["element"]["text"] == "hello world"


@pytest.mark.asyncio
async def test_query_selector_all_paginates(session):
    tab = await session.new_blank_tab()
    await session.navigate(DATA_URL, tab_id=tab.id)

    first = await session.query_selector_all("li.item", tab_id=tab.id, limit=2, offset=0)
    assert first["total_count"] == 3
    assert first["returned"] == 2
    assert first["next_offset"] == 2

    rest = await session.query_selector_all("li.item", tab_id=tab.id, limit=2, offset=2)
    assert rest["returned"] == 1
    assert rest["next_offset"] is None


@pytest.mark.asyncio
async def test_query_selector_invalid_css_is_structured_error(session):
    tab = await session.new_blank_tab()
    await session.navigate(DATA_URL, tab_id=tab.id)

    res = await session.query_selector("div::::bad", tab_id=tab.id)

    assert "invalid CSS selector" in res["error"]
    assert res["tab_id"] == tab.id


@pytest.mark.asyncio
async def test_force_reload_tab(session):
    tab = await session.new_blank_tab()
    await session.navigate(DATA_URL, tab_id=tab.id)
    # Prime the cache, then force a reload and confirm the report.
    await session.get_element_by_id("logo", tab_id=tab.id)

    res = await session.force_reload_tab(tab_id=tab.id)

    assert res["tab_id"] == tab.id
    assert res["reloaded"] is True
    assert res["url"].startswith("data:text/html")

    # The tab still queries correctly after the reload.
    again = await session.get_element_by_id("logo", tab_id=tab.id)
    assert again["found"] is True
