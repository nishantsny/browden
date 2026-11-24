"""End-to-end: an ``upload-file`` rule admits a hidden, id-less file input by its ``accept``.

TikTok Studio's upload page (``www.tiktok.com/tiktokstudio/upload``) has exactly
one file input, ``<input type="file" accept="video/*" style="display: none;">``,
with no id, no name and no label, behind a visible "Select video" button. Before
the ``accept`` rule key nothing could name it: ``hidden_input`` needed
``field_ids``, and there is no id or name to list, so ``upload_file`` refused it
at gate 2 with the hidden/decoy message even under ``label: '.*'``.

The page below mirrors that markup exactly and is served over plain http from
``127.0.0.1`` at the same path, so the rule under test is the one an operator
writes for TikTok, byte for byte apart from the host. The rules are loaded from a
YAML file through the real loader and schema, and the real gate is run, then
ChromeDriver hands the non-displayed input its file. The page's change handler
echoes ``input.files[0]``'s name and size, which proves the browser took it.
"""
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gates import OPEN_READ_GATE

from browden.configs.loader import load_runtime_configuration
from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.mcp.validator import ValidationError, upload_file_gate
from browden.web_navigator.utils.network_utils import get_free_port

UPLOAD_PATH = "/tiktokstudio/upload"

# TikTok Studio's upload page, reduced to what the gates judge: the one file
# input exactly as observed live, and the button a human would click.
_PAGE = """<html><body>
  <div class="upload-card">
    <p>Select video to upload</p>
    <button type="button" onclick="document.querySelector('input[type=file]').click()">Select video</button>
    <input type="file" accept="video/*" style="display: none;">
  </div>
  <div id="state">none</div>
  <script>
    const input = document.querySelector('input[type=file]');
    input.addEventListener('change', () => {
      const f = input.files[0];
      document.getElementById('state').textContent =
        input.files.length + ':' + f.name + ':' + f.size;
    });
  </script>
</body></html>""".encode()

# The same page with a second id-less video input (a post attachment beside the
# one the operator meant): `accept` no longer names one control.
_TWO_INPUT_PAGE = _PAGE.replace(
    b'<div id="state">',
    b'<div id="post"><input type="file" accept="video/*" style="display: none;"></div>\n'
    b'  <div id="state">')

# The live refusal, verbatim.
_HIDDEN_REFUSAL = re.escape(
    "selected element is not a file input (or is a hidden/disabled/readonly/decoy "
    "element) — refusing to upload")

CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        pages = {UPLOAD_PATH: _PAGE, UPLOAD_PATH + "?two": _TWO_INPUT_PAGE}
        if self.path not in pages:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(pages[self.path])

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture
def site():
    """A local HTTP server on 127.0.0.1 serving the upload page; yields its base URL."""
    port = get_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join()


@pytest.fixture
def session(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


@pytest.fixture
def clip(tmp_path):
    location = tmp_path / "videos"
    location.mkdir()
    path = location / "clip.mp4"
    path.write_bytes(CLIP_BYTES)
    return path


def _gate(tmp_path, clip, extra_rule_lines: str):
    """The gate a live server would build from this upload-file rule for 127.0.0.1."""
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(
        "upload-file:\n"
        "  127.0.0.1:\n"
        f"    - path: ['^{UPLOAD_PATH}$']\n"
        "      label: '.*'\n"
        + extra_rule_lines
        + f"allowed_upload_locations: ['{clip.parent}']\n")
    rules = load_runtime_configuration(allowlist).access_rules
    return upload_file_gate(rules, str(clip))


async def _page(session, site, query=""):
    blank = await session.new_blank_tab(max_tabs=10)
    return await session.navigate(site + UPLOAD_PATH + query, id=blank["id"], gate=OPEN_READ_GATE)


async def _state(session, tab_id):
    found = await session.query_selector("#state", id=tab_id, gate=OPEN_READ_GATE)
    return found["element"]["text"]


@pytest.mark.asyncio
async def test_the_hidden_input_is_admitted_by_its_accept_value(session, site, clip, tmp_path):
    page = await _page(session, site)
    # The live rule exactly, its own size cap included.
    gate = _gate(tmp_path, clip, "      hidden_input: true\n      accept: ['video/*']\n"
                                 "      max_bytes: 104857600\n")

    res = await session.upload_file("input[type=file]", id=page["id"], gate=gate)
    assert res["uploaded"] is True
    assert gate.control.hidden is True
    assert await _state(session, page["id"]) == f"1:clip.mp4:{len(CLIP_BYTES)}"


@pytest.mark.asyncio
async def test_without_accept_the_hidden_input_is_refused_as_live(session, site, clip, tmp_path):
    """`label: '.*'` alone: the exact refusal observed on TikTok Studio."""
    page = await _page(session, site)
    gate = _gate(tmp_path, clip, "")

    with pytest.raises(ValidationError, match=_HIDDEN_REFUSAL):
        await session.upload_file("input[type=file]", id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
async def test_a_different_accept_value_is_refused(session, site, clip, tmp_path):
    page = await _page(session, site)
    gate = _gate(tmp_path, clip, "      hidden_input: true\n      accept: ['image/*']\n")

    with pytest.raises(ValidationError, match=_HIDDEN_REFUSAL):
        await session.upload_file("input[type=file]", id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
async def test_a_second_input_with_the_same_accept_value_is_refused(session, site, clip, tmp_path):
    """accept names a type, not a control: with two id-less video inputs on the
    page, a selector that picks just one of them is still refused."""
    page = await _page(session, site, "?two")
    gate = _gate(tmp_path, clip, "      hidden_input: true\n      accept: ['video/*']\n")

    with pytest.raises(ValidationError, match=_HIDDEN_REFUSAL):
        await session.upload_file(".upload-card > input[type=file]", id=page["id"], gate=gate)
    assert await _state(session, page["id"]) == "none"
