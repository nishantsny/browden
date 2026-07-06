# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

"""Tool-level tests for the click write action.

The session is mocked, but the three gates (host allowlist, generic predicate,
per-site label) run for real. The shipped allowlist.yaml keeps click
commented out (default-deny), so gate tests that need an enabled host patch in
_ENABLED_ALLOWLIST — the exact config the commented-out block would enable.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser_guard.mcp.validator import ActionAllowlist, ValidationError

# Mirror of allowlist.yaml with the showcase click block uncommented.
_ENABLED_ALLOWLIST = ActionAllowlist({
    "read": {"*": [".*"]},
    "click": {
        "amazon.com": {"paths": [".*"], "label": r"(?i)\badd to cart\b"},
    },
})


def _atc_node(value="Add to cart", text="", **attrs):
    return {"tag": "input", "id": None, "classes": [],
            "attributes": {"type": "submit", "value": value, **attrs}, "text": text}


def _session(*, url, elements):
    s = MagicMock()
    s.current_url = AsyncMock(return_value=url)
    s.query_selector_all = AsyncMock(
        return_value={"total_count": len(elements), "elements": elements})
    s.click = AsyncMock(
        return_value={"clicked": True, "url": url, "title": "Cart"})
    return s


@pytest.mark.asyncio
async def test_shipped_default_denies_click_everywhere():
    # The click block in the shipped allowlist.yaml is commented out, so
    # even amazon.com is refused until the user opts in.
    import browser_guard.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/B0FBRRM2VQ", elements=[_atc_node()])
    with patch.object(server._store, "route", return_value=session):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.click("#add-to-cart-button", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_happy_path_clicks():
    import browser_guard.mcp.server as server
    importlib = __import__("importlib")
    importlib.reload(server)
    session = _session(url="https://www.amazon.com/dp/B0FBRRM2VQ", elements=[_atc_node()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_ALLOWLIST", _ENABLED_ALLOWLIST):
        result = await server.click("#add-to-cart-button", "h1")
    assert result["clicked"] is True
    session.click.assert_awaited_once_with("#add-to-cart-button", id="h1")


@pytest.mark.asyncio
async def test_host_not_allowed_is_rejected():
    import browser_guard.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://evil.example.com/p", elements=[_atc_node()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_ALLOWLIST", _ENABLED_ALLOWLIST):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.click("#x", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_buy_now_element_is_rejected():
    import browser_guard.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/X",
                       elements=[_atc_node(value="Buy Now")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_ALLOWLIST", _ENABLED_ALLOWLIST):
        with pytest.raises(ValidationError, match="not a recognized add-to-cart"):
            await server.click("#buy-now", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_selector_is_rejected():
    import browser_guard.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/X",
                       elements=[_atc_node(), _atc_node()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_ALLOWLIST", _ENABLED_ALLOWLIST):
        with pytest.raises(ValidationError, match="ambiguous"):
            await server.click(".a-button-input", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_mismatch_for_site_is_rejected():
    # "Add to bag" passes the generic predicate but amazon.com requires "add to cart".
    import browser_guard.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/X",
                       elements=[_atc_node(value="Add to bag")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_ALLOWLIST", _ENABLED_ALLOWLIST):
        with pytest.raises(ValidationError, match="required add-to-cart label"):
            await server.click("#x", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_gone_returns_error():
    import browser_guard.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url=None, elements=[])
    with patch.object(server._store, "route", return_value=session):
        result = await server.click("#x", "h1")
    assert "error" in result and result["id"] == "h1"
    session.click.assert_not_awaited()
