"""Tool-level tests for the insert_text (write-text) action.

The session is mocked, but the three gates (write-text host allowlist, fillable
integrity, per-field visible-label) run for real. write-text is a section
*separate* from click, so enabling one never enables the other.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browden.configs.loader import AllowlistRefresher
from browden.mcp.validator import ActionAllowlist, ValidationError

# amazon.com may type into fields whose visible label reads like "Grocery Tip …".
_ENABLED = ActionAllowlist({
    "read": {"website_overrides": {"*": [".*"]}},
    "write-text": {
        "amazon.com": {"paths": [".*"], "label": r"(?i)grocery tip.*"},
    },
})

def _field(placeholder="Grocery Tip (optional)", tag="input", **attrs):
    return {"tag": tag, "id": None, "classes": [],
            "attributes": {"type": "number", "placeholder": placeholder, **attrs},
            "text": ""}


def _session(*, url, elements):
    s = MagicMock()
    s.document_url = AsyncMock(return_value=url)
    s.query_selector_all = AsyncMock(
        return_value={"total_count": len(elements), "elements": elements})
    s.insert_text = AsyncMock(
        return_value={"inserted": True, "value": "0", "url": url, "title": "Checkout"})
    return s


@pytest.mark.asyncio
async def test_shipped_default_denies_fill_everywhere():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout", elements=[_field()])
    with patch.object(server._store, "route", return_value=session):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.insert_text("#tip", "0", "h1")
    session.insert_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_happy_path_fills():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout", elements=[_field()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_ENABLED)):
        result = await server.insert_text("#tip", "0", "h1")
    assert result["inserted"] is True
    session.insert_text.assert_awaited_once_with("#tip", "0", id="h1")


@pytest.mark.asyncio
async def test_write_text_is_a_separate_section_from_click():
    # A host enabled for `click` is NOT thereby enabled for `insert_text`.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    click_only = ActionAllowlist({
        "read": {"website_overrides": {"*": [".*"]}},
        "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},
    })
    session = _session(url="https://www.amazon.com/checkout", elements=[_field()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(click_only)):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.insert_text("#tip", "0", "h1")
    session.insert_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_text_control_is_rejected():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout",
                       elements=[{"tag": "button", "id": None, "classes": [],
                                  "attributes": {}, "text": "Grocery Tip"}])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_ENABLED)):
        with pytest.raises(ValidationError, match="fillable"):
            await server.insert_text("#b", "0", "h1")
    session.insert_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_field_label_mismatch_is_rejected():
    # A card-number field is fillable, but its visible label doesn't match the
    # host's write-text label, so Gate 3 refuses it.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout",
                       elements=[_field(placeholder="Card number")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_ENABLED)):
        with pytest.raises(ValidationError, match="write-text label"):
            await server.insert_text("#card", "0", "h1")
    session.insert_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_selector_is_rejected():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout", elements=[_field(), _field()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", AllowlistRefresher.static(_ENABLED)):
        with pytest.raises(ValidationError, match="ambiguous"):
            await server.insert_text(".a-input-text", "0", "h1")
    session.insert_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_gone_returns_error():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url=None, elements=[])
    with patch.object(server._store, "route", return_value=session):
        result = await server.insert_text("#x", "0", "h1")
    assert "error" in result and result["id"] == "h1"
    session.insert_text.assert_not_awaited()
