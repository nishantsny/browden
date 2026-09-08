"""End-to-end: clicking a ``<label>`` toggles the CSS-hidden control it is bound to.

Goes through ``BrowserSessionManager.click`` -> ``backend.click_element`` — the
same path the ``click`` MCP tool takes after gating — against the pattern from
issue #146: the native ``<input type=radio|checkbox>`` is hidden in CSS (radios
parked off-screen, the checkbox ``display:none``) and the visible control is a
``::before`` drawn on the ``<label>``.

This is the case where every other route is refused. The input is the wrong tag
for ``click`` and is not rendered, so ``press-key`` refuses it as invisible; the
label is not natively focusable, so ``press-key`` refuses that too. Clicking the
label is what a human does, and this proves it also works through the stack: each
control's ``change`` handler echoes into ``#state``, so re-querying that echo
(the cache is invalidated by the click) proves the toggle really happened.

The page is an inline ``data:`` document, so the run is offline.
"""
import urllib.parse

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager

HTML = """<html><head><style>
  /* the issue #146 pattern, lifted from a real consular-services form */
  [type="radio"] { position: absolute; left: -9999px; }
  [type="radio"] + label { position: relative; padding-left: 28px; cursor: pointer; }
  [type="radio"] + label:before { content: ''; position: absolute; left: 0; top: 0;
    width: 20px; height: 20px; border: 1px solid #5f717d; border-radius: 100%; }
  [type="checkbox"] { display: none; }
  [type="checkbox"] + label:before { content: ''; border: 1px solid #5f717d;
    padding: 10px; display: inline-block; }
</style></head><body>
  <div id="state">none</div>

  <p><input type="radio" id="stage1" name="stage" value="1">
     <label for="stage1">Before submitting the physical application</label></p>
  <p><input type="radio" id="stage2" name="stage" value="2">
     <label for="stage2">After submitting the physical application</label></p>

  <p><input type="checkbox" id="consent" name="consent">
     <label for="consent">I consent to the processing of my personal data</label></p>

  <script>
    const state = document.getElementById('state');
    document.querySelectorAll('input').forEach(el => {
      el.addEventListener('change', () => {
        state.textContent = el.id + ':' + (el.checked ? 'on' : 'off');
      });
    });
  </script>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


async def _state(session, tab_id):
    found = await session.query_selector("#state", id=tab_id)
    return found["element"]["text"]


@pytest.mark.asyncio
async def test_clicking_a_label_selects_its_hidden_radio(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    assert await _state(session, page["id"]) == "none"

    await session.click('label[for="stage2"]', id=page["id"])
    assert await _state(session, page["id"]) == "stage2:on"


@pytest.mark.asyncio
async def test_clicking_a_label_ticks_its_display_none_checkbox(session):
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    await session.click('label[for="consent"]', id=page["id"])
    assert await _state(session, page["id"]) == "consent:on"


@pytest.mark.asyncio
async def test_radio_group_switches_between_labels(session):
    """Selecting a second option moves the group, as a real radio group must."""
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    await session.click('label[for="stage1"]', id=page["id"])
    assert await _state(session, page["id"]) == "stage1:on"

    await session.click('label[for="stage2"]', id=page["id"])
    assert await _state(session, page["id"]) == "stage2:on"


@pytest.mark.asyncio
async def test_the_hidden_input_itself_is_still_unreachable(session):
    """Why the label route is needed at all: the input cannot be clicked or pressed."""
    blank = await session.new_blank_tab(max_tabs=10)
    page = await session.navigate(DATA_URL, id=blank["id"])

    with pytest.raises(ValueError, match="not visible"):
        await session.click("#stage2", id=page["id"])
    with pytest.raises(ValueError, match="not visible"):
        await session.press_key("#stage2", "Space", id=page["id"])
