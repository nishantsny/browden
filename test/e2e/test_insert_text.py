"""End-to-end: the insert_text (write-text) primitive against a real (headless) Chrome.

Goes through ``BrowserSessionManager.insert_text`` -> ``backend.insert_text_element`` — the same
path the ``insert_text`` MCP tool takes after gating — so it exercises the live chain:
find one element -> visible/enabled -> ``clear()`` + ``send_keys``. Each field
echoes its live value on ``input`` into an ``<output>``; re-querying that echo
(the cache is invalidated by the write) proves the value actually landed and input
events fired. The page is an inline ``data:`` document, so the run is offline.
"""
import urllib.parse

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager

HTML = """<html><body>
  <input id="tip" type="number" value="5" placeholder="Grocery Tip">
  <textarea id="note">old text</textarea>
  <output id="tipecho"></output>
  <output id="noteecho"></output>
  <script>
    tip.addEventListener('input', () => { tipecho.textContent = tip.value; });
    note.addEventListener('input', () => { noteecho.textContent = note.value; });
  </script>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


@pytest.mark.asyncio
async def test_fill_clears_and_replaces_number_input(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    res = await session.insert_text("#tip", "0", id=page["id"])
    assert res["inserted"] is True
    assert res["value"] == "0"
    assert res["id"] == page["id"]

    # The echo reflects the field's live value: clear() wiped the "5" and "0" was
    # typed (so the result is "0", not "50").
    echo = await session.query_selector("#tipecho", id=page["id"])
    assert echo["found"] is True
    assert echo["element"]["text"] == "0"


@pytest.mark.asyncio
async def test_fill_replaces_textarea_content(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    await session.insert_text("#note", "leave at door", id=page["id"])

    echo = await session.query_selector("#noteecho", id=page["id"])
    assert echo["found"] is True
    assert echo["element"]["text"] == "leave at door"


@pytest.mark.asyncio
async def test_fill_refuses_ambiguous_selector(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    # Two <output> elements match — the backend refuses rather than type into an
    # arbitrary one (the DOM moved under a snapshot that had validated one match).
    with pytest.raises(ValueError, match="matched 2 live elements"):
        await session.insert_text("output", "x", id=page["id"])
