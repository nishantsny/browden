"""End-to-end: clicking an ``<a>`` anchor (a newly-accepted click target) in real Chrome.

Exercises the live click path — ``BrowserSessionManager.click`` -> backend -> a
real Selenium element click — on an ``<a>``, which the click guard began
accepting. Mirrors the Amazon grocery-tip flow at the mechanism level: a
``javascript:`` "Edit" anchor reveals a label-less ``<input>``, then insert_text
zeroes it. The page is an inline ``data:`` document, so the run is offline.

(The *gating* — anchor target on the read allowlist, and field_ids authorizing a
label-less box — is covered by the unit suites; this proves the real browser
actually clicks an anchor and the revealed field then accepts text.)
"""
import urllib.parse

import pytest

from browden.web_navigator.selenium_chrome import SeleniumChromeBackend
from browden.mcp.session_management.BrowserSessionManager import BrowserSessionManager

HTML = """<html><body>
  <a id="edit" href="javascript:void(0)"
     onclick="document.getElementById('form').style.display='block';
              document.getElementById('state').textContent='shown';">Edit</a>
  <div id="form" style="display:none">
    <input id="amt" type="number" value="5" oninput="amtecho.textContent = amt.value;">
  </div>
  <output id="state">hidden</output>
  <output id="amtecho"></output>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def session(tmp_path):
    backend = SeleniumChromeBackend(profile_dir=str(tmp_path / "profile"))
    s = BrowserSessionManager(backend, namespace="e2e", start_reaper=False)
    yield s
    backend._drv().quit()


@pytest.mark.asyncio
async def test_click_fires_anchor_onclick(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    # The reveal control is an <a> (href="javascript:void(0)"). Clicking it must
    # fire its onclick — the field starts hidden and becomes shown.
    res = await session.click("#edit", id=page["id"])
    assert res["clicked"] is True

    state = await session.query_selector("#state", id=page["id"])
    assert state["found"] is True
    assert state["element"]["text"] == "shown"


@pytest.mark.asyncio
async def test_anchor_reveals_then_insert_text_zeroes_labelless_field(session):
    # The exact grocery-tip shape: click the "Edit" anchor to un-hide a label-less
    # number <input>, then type "0" into it. clear() wipes the "5" so the echo
    # reads "0", not "50".
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    await session.click("#edit", id=page["id"])

    res = await session.insert_text("#amt", "0", id=page["id"])
    assert res["inserted"] is True
    assert res["value"] == "0"

    echo = await session.query_selector("#amtecho", id=page["id"])
    assert echo["found"] is True
    assert echo["element"]["text"] == "0"
