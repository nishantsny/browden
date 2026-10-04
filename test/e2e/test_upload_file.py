"""End-to-end: the upload_file (upload-file) primitive against a real (headless) Chrome.

Goes through ``BrowserSessionManager.upload_file`` -> ``GatedPage`` ->
``backend.upload_file_target`` — the same path the ``upload_file`` MCP tool takes
— so it exercises the live chain: snapshot the one match -> visible/enabled ->
``send_keys(path)``. The page echoes the file the input actually received (its
name and its decoded bytes) on ``change``, so re-querying that echo proves the
browser really took the file and fired the page's handler, which is the whole
point: no native file dialog is involved.

The last two tests run the *real* gate rather than the pass-through one, because
the filesystem root is the gate with no analogue in the other write actions — a
refusal there is the difference between "attach a receipt" and "exfiltrate
~/.ssh/id_rsa". The page is an inline ``data:`` document, so the run is offline.
"""
import urllib.parse

import pytest
from gates import OPEN_GATE, OPEN_READ_GATE

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.mcp.validator import (
    BrowdenAccessRuleSet,
    ValidationError,
    upload_file_gate,
)

HTML = """<html><body>
  <div class="upload">
    <p>Attach an image or PDF:</p>
    <input id="bill_file_expense" type="file"/>
  </div>
  <input id="other" type="file"/>
  <div id="state">none</div>
  <script>
    const state = document.getElementById('state');
    document.querySelectorAll('input[type=file]').forEach(el => {
      el.addEventListener('change', () => {
        const f = el.files[0];
        const reader = new FileReader();
        reader.onload = () => { state.textContent = el.id + ':' + f.name + ':' + reader.result; };
        reader.readAsText(f);
      });
    });
  </script>
</body></html>"""

DATA_URL = "data:text/html," + urllib.parse.quote(HTML)


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


@pytest.fixture
def receipt(tmp_path):
    """A file under a root, plus a secret outside it."""
    root = tmp_path / "receipts"
    root.mkdir()
    path = root / "lunch.txt"
    path.write_text("LUNCH-RECEIPT")
    (tmp_path / "id_rsa").write_text("PRIVATE KEY")
    return path


async def _state(session, tab_id):
    found = await session.query_selector("#state", id=tab_id, gate=OPEN_READ_GATE)
    return found["element"]["text"]


async def _page(session):
    blank = await session.new_blank_tab(max_tabs=10)
    return await session.navigate(DATA_URL, id=blank["id"], gate=OPEN_READ_GATE)


def _real_gate(root, file_path):
    """The gate a live server would build: any host, any label, one upload root."""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"*": {"paths": [".*"], "label": ".*"}},
        "upload_roots": [str(root)],
    })
    return upload_file_gate(rules, str(file_path))


@pytest.mark.asyncio
async def test_the_file_input_receives_the_file(session, receipt):
    page = await _page(session)
    assert await _state(session, page["id"]) == "none"

    res = await session.upload_file("#bill_file_expense", str(receipt),
                                    id=page["id"], gate=OPEN_GATE)
    assert res["uploaded"] is True
    assert res["id"] == page["id"]

    # The page's own change handler read the file out of the input: the browser
    # really took it, with its name and its bytes.
    assert await _state(session, page["id"]) == "bill_file_expense:lunch.txt:LUNCH-RECEIPT"


@pytest.mark.asyncio
async def test_an_ambiguous_selector_uploads_nothing(session, receipt):
    page = await _page(session)
    with pytest.raises(ValueError, match="ambiguous|matched 2"):
        await session.upload_file("input[type=file]", str(receipt),
                                  id=page["id"], gate=OPEN_GATE)
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
async def test_the_real_gate_admits_a_file_under_an_upload_root(session, receipt):
    page = await _page(session)
    gate = _real_gate(receipt.parent, receipt)
    await session.upload_file("#bill_file_expense", str(receipt), id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "bill_file_expense:lunch.txt:LUNCH-RECEIPT"


@pytest.mark.asyncio
async def test_the_real_gate_refuses_a_file_outside_the_upload_roots(session, receipt, tmp_path):
    """Nothing reaches the page: the file gate runs before the DOM is read."""
    page = await _page(session)
    secret = tmp_path / "id_rsa"
    gate = _real_gate(receipt.parent, secret)
    with pytest.raises(ValidationError, match="outside every configured upload root"):
        await session.upload_file("#bill_file_expense", str(secret), id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "none"
