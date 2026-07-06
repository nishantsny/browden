"""End-to-end: the DOM-query + force_reload tools against a real (headless) Chrome.

These go through ``BrowserSessionManager`` — the same path the MCP tools take — so they
exercise the full chain: live Selenium ``get_page_source`` -> soup cache ->
``dom.query`` -> ``dom.serialize`` node. The page is an inline ``data:``
document, so the run is deterministic and offline.
"""
import urllib.parse

import pytest

from browden.web_navigator.selenium_chrome import SeleniumChromeBackend
from browden.mcp.session_management.BrowserSessionManager import BrowserSessionManager

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
def session(tmp_path):
    backend = SeleniumChromeBackend(profile_dir=str(tmp_path / "profile"))
    s = BrowserSessionManager(backend, namespace="e2e", start_reaper=False)
    yield s
    backend._drv().quit()


@pytest.mark.asyncio
async def test_get_element_by_id(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    res = await session.get_element_by_id("logo", id=page["id"])

    assert res["found"] is True
    assert res["element"]["id"] == "logo"
    assert res["element"]["classes"] == ["brand", "mark"]
    assert res["element"]["text"] == "BG"

    missing = await session.get_element_by_id("nope", id=page["id"])
    assert missing["found"] is False
    assert missing["element"] is None


@pytest.mark.asyncio
async def test_get_elements_by_class_name(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    res = await session.get_elements_by_class_name("item js-item", id=page["id"])

    assert res["total_count"] == 3
    assert res["returned"] == 3
    assert all("item" in e["classes"] for e in res["elements"])
    assert {e["text"] for e in res["elements"]} == {"alpha", "beta", "gamma"}


@pytest.mark.asyncio
async def test_query_selector_single_match(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    res = await session.query_selector("p.note", id=page["id"])

    assert res["found"] is True
    assert res["element"]["tag"] == "p"
    assert res["element"]["text"] == "hello world"


@pytest.mark.asyncio
async def test_query_selector_all_paginates(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    first = await session.query_selector_all("li.item", id=page["id"], limit=2, offset=0)
    assert first["total_count"] == 3
    assert first["returned"] == 2
    assert first["next_offset"] == 2

    rest = await session.query_selector_all("li.item", id=page["id"], limit=2, offset=2)
    assert rest["returned"] == 1
    assert rest["next_offset"] is None


@pytest.mark.asyncio
async def test_query_selector_invalid_css_is_structured_error(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    res = await session.query_selector("div::::bad", id=page["id"])

    assert "invalid CSS selector" in res["error"]
    assert res["id"] == page["id"]


@pytest.mark.asyncio
async def test_force_reload_page(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])
    # Prime the cache, then force a reload and confirm the report.
    await session.get_element_by_id("logo", id=page["id"])

    res = await session.force_reload_tab(id=page["id"])

    assert res["id"] == page["id"]
    assert res["reloaded"] is True
    assert res["url"].startswith("data:text/html")

    # The page still queries correctly after the reload.
    again = await session.get_element_by_id("logo", id=page["id"])
    assert again["found"] is True
