"""End-to-end: the upload_file (upload-file) primitive against a real (headless) Chrome.

Goes through ``BrowserSessionManager.upload_file`` -> ``GatedPage`` ->
``backend.upload_file_target`` — the same path the ``upload_file`` MCP tool takes
— so it exercises the live chain: snapshot the one match -> visible/enabled ->
``send_keys(path)``. The page echoes the file the input actually received (its
name and its decoded bytes) on ``change``, so re-querying that echo proves the
browser really took the file and fired the page's handler, which is the whole
point: no native file dialog is involved.

Two tests run the *real* gate rather than the pass-through one, because the
allowed-location check is the gate with no analogue in the other actions — a
refusal there is the difference between "attach a receipt" and "exfiltrate
~/.ssh/id_rsa". The pages are inline ``data:`` documents, so the run is offline.

The hidden-input tests pin the shape almost every real upload page uses, YouTube
Studio's among them: the native ``<input type=file multiple>`` is ``display:none``
+ ``aria-hidden`` and the page draws its own "Select files" button. They run the
real gate too, since what is under test is both halves of the opt-in: the gate
admits the hidden input only through a ``hidden_input`` rule, and ChromeDriver
then really does hand a non-displayed file input its file.
"""
import urllib.parse

import pytest
from gates import OPEN_READ_GATE, open_upload_gate

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

# YouTube Studio's upload dialog, reduced to the parts the gates judge: the real
# control (attributes as observed 2026-11-22) and the button a human would click.
HIDDEN_HTML = """<html><body>
  <p>Drag and drop video files to upload</p>
  <button id="select-files-button" onclick="document.querySelector('input').click()">Select files</button>
  <input type="file" name="Filedata" multiple aria-hidden="true" tabindex="-1"
         style="position: absolute; overflow: hidden; height: 0px; width: 0px; opacity: 0; left: 0px; top: 0px; display: none;"/>
  <div id="state">none</div>
  <script>
    const input = document.querySelector('input[type=file]');
    const state = document.getElementById('state');
    input.addEventListener('change', () => {
      const f = input.files[0];
      const reader = new FileReader();
      reader.onload = () => { state.textContent = input.files.length + ':' + f.name + ':' + reader.result; };
      reader.readAsText(f);
    });
  </script>
</body></html>"""

HIDDEN_DATA_URL = "data:text/html," + urllib.parse.quote(HIDDEN_HTML)

# The same input hidden where the snapshot cannot see it: by a stylesheet class,
# and by a hidden ancestor. Its own attributes look like a visible control.
_PLAIN_INPUT = '<input type="file" name="Filedata" multiple{attrs}/>'
CLASS_HIDDEN_DATA_URL = "data:text/html," + urllib.parse.quote(HIDDEN_HTML.replace(
    HIDDEN_HTML[HIDDEN_HTML.index("<input"):HIDDEN_HTML.index("/>") + 2],
    "<style>.sr-only { display: none; }</style>" + _PLAIN_INPUT.format(attrs=' class="sr-only"')))
ANCESTOR_HIDDEN_DATA_URL = "data:text/html," + urllib.parse.quote(HIDDEN_HTML.replace(
    HIDDEN_HTML[HIDDEN_HTML.index("<input"):HIDDEN_HTML.index("/>") + 2],
    '<div style="display: none">' + _PLAIN_INPUT.format(attrs="") + "</div>"))


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


@pytest.fixture
def receipt(tmp_path):
    """A file under an allowed location, plus a secret outside it."""
    location = tmp_path / "receipts"
    location.mkdir()
    path = location / "lunch.txt"
    path.write_text("LUNCH-RECEIPT")
    (tmp_path / "id_rsa").write_text("PRIVATE KEY")
    return path


async def _state(session, tab_id):
    found = await session.query_selector("#state", id=tab_id, gate=OPEN_READ_GATE)
    return found["element"]["text"]


async def _page(session, url=DATA_URL):
    blank = await session.new_blank_tab(max_tabs=10)
    return await session.navigate(url, id=blank["id"], gate=OPEN_READ_GATE)


def _real_gate(location, file_path):
    """The gate a live server would build: any host, any label, one allowed upload location."""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"*": {"paths": [".*"], "label": ".*"}},
        "allowed_upload_locations": [str(location)],
    })
    return upload_file_gate(rules, str(file_path))


@pytest.mark.asyncio
async def test_the_file_input_receives_the_file(session, receipt):
    page = await _page(session)
    assert await _state(session, page["id"]) == "none"

    res = await session.upload_file("#bill_file_expense", id=page["id"],
                                    gate=open_upload_gate(receipt))
    assert res["uploaded"] is True
    assert res["id"] == page["id"]

    # The page's own change handler read the file out of the input: the browser
    # really took it, with its name and its bytes.
    assert await _state(session, page["id"]) == "bill_file_expense:lunch.txt:LUNCH-RECEIPT"


@pytest.mark.asyncio
async def test_an_ambiguous_selector_uploads_nothing(session, receipt):
    page = await _page(session)
    with pytest.raises(ValueError, match="ambiguous|matched 2"):
        await session.upload_file("input[type=file]", id=page["id"],
                                  gate=open_upload_gate(receipt))
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
async def test_the_real_gate_admits_a_file_under_an_allowed_location(session, receipt):
    page = await _page(session)
    gate = _real_gate(receipt.parent, receipt)
    await session.upload_file("#bill_file_expense", id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "bill_file_expense:lunch.txt:LUNCH-RECEIPT"


@pytest.mark.asyncio
async def test_the_real_gate_refuses_a_file_outside_the_allowed_upload_locations(session, receipt, tmp_path):
    """Nothing reaches the page: the file gate runs before the DOM is read."""
    page = await _page(session)
    secret = tmp_path / "id_rsa"
    gate = _real_gate(receipt.parent, secret)
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        await session.upload_file("#bill_file_expense", id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "none"


def _hidden_input_gate(location, file_path, *, hidden_input):
    """A live-server gate naming the input by name, with or without the hidden opt-in.

    The label regex matches the "Select files" text on purpose: it must not be
    what admits the hidden input.
    """
    rule = {"paths": [".*"], "label": "(?i)select files", "field_ids": ["Filedata"]}
    if hidden_input:
        rule["hidden_input"] = True
    rules = BrowdenAccessRuleSet({
        "upload-file": {"*": rule},
        "allowed_upload_locations": [str(location)],
    })
    return upload_file_gate(rules, str(file_path))


@pytest.mark.asyncio
async def test_an_opted_in_hidden_multiple_input_receives_the_file(session, receipt):
    page = await _page(session, HIDDEN_DATA_URL)
    gate = _hidden_input_gate(receipt.parent, receipt, hidden_input=True)

    res = await session.upload_file("input[type=file][name=Filedata]", id=page["id"], gate=gate)
    assert res["uploaded"] is True
    assert gate.control.hidden is True

    # The page's change handler saw exactly one file, with its name and bytes,
    # although the input was never displayed.
    assert await _state(session, page["id"]) == "1:lunch.txt:LUNCH-RECEIPT"


@pytest.mark.asyncio
async def test_a_hidden_input_without_the_opt_in_uploads_nothing(session, receipt):
    page = await _page(session, HIDDEN_DATA_URL)
    gate = _hidden_input_gate(receipt.parent, receipt, hidden_input=False)

    with pytest.raises(ValidationError, match="not a file input"):
        await session.upload_file("input[type=file][name=Filedata]", id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
async def test_the_backend_still_refuses_a_hidden_input_the_gate_did_not_admit_as_hidden(
        session, receipt):
    """The live "is displayed" check is waived by the gate's verdict alone.

    The pass-through gate judges nothing, so it never records a hidden
    admission, and the backend's own check refuses the non-displayed input.
    """
    page = await _page(session, HIDDEN_DATA_URL)
    with pytest.raises(ValueError, match="not visible"):
        await session.upload_file("input[type=file][name=Filedata]", id=page["id"],
                                  gate=open_upload_gate(receipt))
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [CLASS_HIDDEN_DATA_URL, ANCESTOR_HIDDEN_DATA_URL],
                         ids=["css-class", "hidden-ancestor"])
async def test_an_opted_in_input_hidden_where_the_snapshot_cannot_see_receives_the_file(
        session, receipt, url):
    page = await _page(session, url)
    gate = _hidden_input_gate(receipt.parent, receipt, hidden_input=True)
    await session.upload_file("input[type=file][name=Filedata]", id=page["id"], gate=gate)
    assert gate.control.hidden is True
    assert await _state(session, page["id"]) == "1:lunch.txt:LUNCH-RECEIPT"


@pytest.mark.asyncio
async def test_a_second_upload_to_a_multiple_input_replaces_the_first(session, receipt):
    """ChromeDriver appends to a `multiple` input; the backend empties it first,
    so the input never holds more than the one file this call admitted."""
    dinner = receipt.parent / "dinner.txt"
    dinner.write_text("DINNER-RECEIPT")
    page = await _page(session, HIDDEN_DATA_URL)
    selector = "input[type=file][name=Filedata]"

    await session.upload_file(selector, id=page["id"],
                              gate=_hidden_input_gate(receipt.parent, receipt, hidden_input=True))
    assert await _state(session, page["id"]) == "1:lunch.txt:LUNCH-RECEIPT"

    await session.upload_file(selector, id=page["id"],
                              gate=_hidden_input_gate(receipt.parent, dinner, hidden_input=True))
    assert await _state(session, page["id"]) == "1:dinner.txt:DINNER-RECEIPT"
