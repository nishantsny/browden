"""Tool-level tests for the upload_file (upload-file) action.

The session is mocked, but all four gates run for real: the upload-file host
allowlist, the allowed-location gate, file-input integrity, and the per-control
label / field_ids check. ``upload-file`` is a section of its own — a host trusted
to type is never thereby trusted to hand a website a local file.
"""
from unittest.mock import MagicMock, patch

import pytest
from gated_fakes import gated_write

from browden.configs.loader import RuntimeConfigurationRefresher
from browden.mcp.validator import BrowdenRuntimeConfiguration, ValidationError

SPLITWISE = "https://secure.splitwise.com/"


@pytest.fixture
def receipts(tmp_path):
    """One allowed upload location with a receipt in it, and a secret outside it."""
    location = tmp_path / "receipts"
    location.mkdir()
    (location / "lunch.png").write_bytes(b"png")
    (tmp_path / "id_rsa").write_bytes(b"PRIVATE KEY")
    return tmp_path


def _enabled(receipts, **extra):
    return BrowdenRuntimeConfiguration({
        "read": {"website_overrides": {"*": [".*"]}},
        "allowed_upload_locations": [str(receipts / "receipts")],
        "upload-file": {"secure.splitwise.com": [
            {"path": [".*"], "label": r"(?i)receipt", "field_ids": ["bill_file_expense"]}]},
        **extra,
    })


def _file_input(node_id="bill_file_expense", **attrs):
    return {"tag": "input", "id": node_id, "classes": [],
            "attributes": {"type": "file", **attrs}, "text": ""}


def _session(*, url, elements, file_path=""):
    s = MagicMock()
    s.upload_file = gated_write(
        url=url, elements=elements,
        result={"uploaded": True, "file_path": file_path, "url": url, "title": "Splitwise"})
    return s


def _server():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    return server


async def test_shipped_default_denies_upload_everywhere():
    server = _server()
    session = _session(url=SPLITWISE, elements=[_file_input()])
    with patch.object(server._store, "route", return_value=session):
        with pytest.raises(ValidationError, match="not allowed on this page"):
            await server.upload_file("#bill_file_expense", "/tmp/x.png", "h1")
    assert session.upload_file.performed == []


async def test_happy_path_uploads(receipts):
    server = _server()
    path = str(receipts / "receipts" / "lunch.png")
    session = _session(url=SPLITWISE, elements=[_file_input()], file_path=path)
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(_enabled(receipts))):
        result = await server.upload_file("#bill_file_expense", path, "h1")
    assert result["uploaded"] is True
    assert session.upload_file.performed == [("#bill_file_expense", path)]


async def test_a_file_outside_the_roots_is_refused_before_the_page_is_touched(receipts):
    """The gate that has no analogue elsewhere: an upload is also a local read."""
    server = _server()
    session = _session(url=SPLITWISE, elements=[_file_input()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(_enabled(receipts))):
        with pytest.raises(ValidationError, match="outside every allowed upload location"):
            await server.upload_file("#bill_file_expense", str(receipts / "id_rsa"), "h1")
    assert session.upload_file.performed == []


async def test_an_authorized_host_with_no_roots_uploads_nothing(receipts):
    """Both halves are required: the host rule alone authorizes no file."""
    server = _server()
    configuration = BrowdenRuntimeConfiguration({
        "read": {"website_overrides": {"*": [".*"]}},
        "upload-file": {"secure.splitwise.com": {"paths": [".*"], "label": ".*"}},
    })
    session = _session(url=SPLITWISE, elements=[_file_input()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(configuration)):
        with pytest.raises(ValidationError, match="no allowed_upload_locations are configured"):
            await server.upload_file("#bill_file_expense", str(receipts / "receipts" / "lunch.png"), "h1")
    assert session.upload_file.performed == []


async def test_upload_file_is_a_separate_section_from_write_text(receipts):
    """The escalation this action exists to avoid: typing never implies uploading."""
    server = _server()
    typing_only = BrowdenRuntimeConfiguration({
        "read": {"website_overrides": {"*": [".*"]}},
        "allowed_upload_locations": [str(receipts / "receipts")],
        "write-text": {"secure.splitwise.com": {"paths": [".*"], "label": ".*"}},
    })
    session = _session(url=SPLITWISE, elements=[_file_input()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(typing_only)):
        with pytest.raises(ValidationError, match="no upload-file rule authorizes"):
            await server.upload_file("#bill_file_expense", str(receipts / "receipts" / "lunch.png"), "h1")
    assert session.upload_file.performed == []


async def test_a_text_box_named_by_the_rule_is_still_not_a_file_channel(receipts):
    """field_ids names a control; it does not make a text input uploadable."""
    server = _server()
    text_box = {"tag": "input", "id": "bill_file_expense", "classes": [],
                "attributes": {"type": "text"}, "text": ""}
    session = _session(url=SPLITWISE, elements=[text_box])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(_enabled(receipts))):
        with pytest.raises(ValidationError, match="not a file input"):
            await server.upload_file("#bill_file_expense", str(receipts / "receipts" / "lunch.png"), "h1")
    assert session.upload_file.performed == []


async def test_a_denied_host_is_refused_even_with_a_rule(receipts):
    server = _server()
    denied = _enabled(receipts, denylist={"secure.splitwise.com": [".*"]})
    session = _session(url=SPLITWISE, elements=[_file_input()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(denied)):
        with pytest.raises(ValidationError, match="denylist"):
            await server.upload_file("#bill_file_expense", str(receipts / "receipts" / "lunch.png"), "h1")
    assert session.upload_file.performed == []
