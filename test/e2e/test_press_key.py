"""End-to-end: the press-key primitive against a real (headless) Chrome.

Goes through ``BrowserSessionManager.press_key`` -> ``backend.press_key_element`` —
the same path the ``press_key`` MCP tool takes after gating — so it exercises the
live chain: find one element -> visible/enabled -> JS ``focus()`` + ``send_keys``
of a control key. The page is a keyboard-operable list of ``<li tabindex>`` rows
(the shape a coordinate ``click`` can't reach): each row's ``keydown`` handler
echoes what happened into ``#picked``, so re-querying that echo (the cache is
invalidated by the press) proves the key actually reached the focused element.
The page is an inline ``data:`` document, so the run is offline.
"""
import urllib.parse

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager

HTML = """<html><body>
  <div id="picked"></div>
  <ul id="list">
    <li id="r1" class="row" tabindex="0">Egg</li>
    <li id="r2" class="row" tabindex="-1">Avocado</li>
    <li id="r3" class="row" tabindex="-1">Quinoa</li>
  </ul>
  <script>
    const picked = document.getElementById('picked');
    document.querySelectorAll('.row').forEach(row => {
      row.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          picked.textContent = 'picked:' + row.textContent;
        } else if (e.key === 'ArrowDown' && row.nextElementSibling) {
          row.nextElementSibling.focus();
          picked.textContent = 'focus:' + row.nextElementSibling.textContent;
        }
      });
    });
  </script>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


@pytest.mark.asyncio
async def test_enter_activates_focusable_row(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    # A <li tabindex="0"> is not a <button>/<a>, so click can't reach it — but
    # press-key focuses it and Enter fires its keydown handler.
    res = await session.press_key("#r1", "Enter", id=page["id"])
    assert res["pressed"] is True
    assert res["key"] == "Enter"
    assert res["id"] == page["id"]

    echo = await session.query_selector("#picked", id=page["id"])
    assert echo["found"] is True
    assert echo["element"]["text"] == "picked:Egg"


@pytest.mark.asyncio
async def test_arrow_key_moves_selection(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    # ArrowDown on the first row moves focus to the next (roving tabindex) — proves
    # a navigation key is delivered to the focused element, not just Enter.
    await session.press_key("#r1", "ArrowDown", id=page["id"])
    echo = await session.query_selector("#picked", id=page["id"])
    assert echo["element"]["text"] == "focus:Avocado"


@pytest.mark.asyncio
async def test_unsupported_key_refused(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    # The backend only maps control keys; a character key never reaches Chrome here
    # (the MCP gate refuses it earlier too, but the primitive is defence in depth).
    with pytest.raises(ValueError, match="unsupported key"):
        await session.press_key("#r1", "a", id=page["id"])


@pytest.mark.asyncio
async def test_ambiguous_selector_refused(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    # Three .row elements match — the backend refuses rather than press a key on an
    # arbitrary one (the DOM moved under a snapshot that had validated one match).
    with pytest.raises(ValueError, match="matched 3 live elements"):
        await session.press_key(".row", "Enter", id=page["id"])
