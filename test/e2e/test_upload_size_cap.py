"""End-to-end: the upload-file size cap against a real (headless) Chrome.

The live case behind it: a 72 MB video refused by what used to be a hard-coded
25 MiB cap, with no way to raise it. The cap is now config — a global
``max_upload_bytes`` default (built in at 1 MiB) and a per-rule ``max_bytes``
override — and these tests run the *real* gate through
``BrowserSessionManager.upload_file`` -> ``GatedPage`` -> the backend, the path
the ``upload_file`` tool takes, so a refusal is proven to leave the input empty
and an admission is proven to reach the page.

The page echoes what the input actually holds on ``change`` (the file's name
and its size from ``input.files``), without reading the bytes. The files are
sparse (``truncate``), so a "just over 1 MiB" file costs no disk and the run
stays fast; ChromeDriver hands a local driver the path, not the contents.
"""
import urllib.parse

import pytest
from gates import OPEN_READ_GATE

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.mcp.validator import (
    DEFAULT_MAX_UPLOAD_BYTES,
    BrowdenAccessRuleSet,
    ValidationError,
    upload_file_gate,
)

MIB = 1024 * 1024

HTML = """<html><body>
  <p>Upload a video:</p>
  <input id="video" type="file"/>
  <p>Attach a receipt:</p>
  <input id="receipt" type="file"/>
  <div id="state">none</div>
  <script>
    const state = document.getElementById('state');
    document.querySelectorAll('input[type=file]').forEach(input => {
      input.addEventListener('change', () => {
        const f = input.files[0];
        state.textContent = input.files.length + ':' + f.name + ':' + f.size;
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
def location(tmp_path):
    path = tmp_path / "uploads"
    path.mkdir()
    return path


def _sparse(path, size):
    with path.open("wb") as fh:
        fh.truncate(size)
    return path


def _gate(location, file_path, *, rule_max_bytes=None):
    """The gate a live server would build from a config with no size settings,
    optionally with ``max_bytes`` on the one upload-file rule."""
    rule = {"paths": [".*"], "label": ".*", "field_ids": ["video"]}
    if rule_max_bytes is not None:
        rule["max_bytes"] = rule_max_bytes
    rules = BrowdenAccessRuleSet({
        "upload-file": {"*": rule},
        "allowed_upload_locations": [str(location)],
    })
    return upload_file_gate(rules, str(file_path))


async def _page(session):
    blank = await session.new_blank_tab(max_tabs=10)
    return await session.navigate(DATA_URL, id=blank["id"], gate=OPEN_READ_GATE)


async def _state(session, tab_id):
    found = await session.query_selector("#state", id=tab_id, gate=OPEN_READ_GATE)
    return found["element"]["text"]


@pytest.mark.asyncio
async def test_with_no_size_config_a_file_just_over_one_mib_is_refused(session, location):
    assert DEFAULT_MAX_UPLOAD_BYTES == MIB
    video = _sparse(location / "clip.mp4", MIB + 1)
    page = await _page(session)
    with pytest.raises(ValidationError, match=rf"is {MIB + 1} bytes, over the {MIB}-byte upload cap"):
        await session.upload_file("#video", id=page["id"], gate=_gate(location, video))
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
async def test_a_rule_max_bytes_above_the_file_lets_the_same_file_through(session, location):
    video = _sparse(location / "clip.mp4", MIB + 1)
    page = await _page(session)
    res = await session.upload_file("#video", id=page["id"],
                                    gate=_gate(location, video, rule_max_bytes=2 * MIB))
    assert res["uploaded"] is True
    assert await _state(session, page["id"]) == f"1:clip.mp4:{MIB + 1}"


@pytest.mark.asyncio
async def test_a_file_over_the_rule_max_bytes_is_refused(session, location):
    video = _sparse(location / "long.mp4", 2 * MIB + 1)
    page = await _page(session)
    with pytest.raises(ValidationError, match=f"over the {2 * MIB}-byte upload cap"):
        await session.upload_file("#video", id=page["id"],
                                  gate=_gate(location, video, rule_max_bytes=2 * MIB))
    assert await _state(session, page["id"]) == "none"


@pytest.mark.asyncio
async def test_a_bigger_cap_on_another_rule_is_not_lent_to_this_control(session, location):
    """The pre-DOM check passes (the video rule's 2 MiB cap is on this page), but
    the receipt input is admitted only by the receipt rule, at the 1 MiB default:
    the element check refuses, after the DOM was read, and nothing is attached."""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"*": [
            {"path": [".*"], "label": "(?!)", "field_ids": ["video"], "max_bytes": 2 * MIB},
            {"path": [".*"], "label": "(?!)", "field_ids": ["receipt"]}]},
        "allowed_upload_locations": [str(location)],
    })
    scan = _sparse(location / "scan.pdf", MIB + MIB // 2)
    page = await _page(session)
    with pytest.raises(ValidationError, match=f"over the {MIB}-byte upload cap"):
        await session.upload_file("#receipt", id=page["id"],
                                  gate=upload_file_gate(rules, str(scan)))
    assert await _state(session, page["id"]) == "none"
    # The same file into the control the bigger-cap rule admits goes through.
    await session.upload_file("#video", id=page["id"], gate=upload_file_gate(rules, str(scan)))
    assert await _state(session, page["id"]) == f"1:scan.pdf:{MIB + MIB // 2}"
