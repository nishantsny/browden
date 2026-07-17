"""Tool-level tests for the click write action.

The session is mocked, but the three gates (host allowlist, generic predicate,
per-site label) run for real. The shipped allowlist.yaml keeps click
commented out (default-deny), so gate tests that need an enabled host patch in
_ENABLED_ALLOWLIST — the exact config the commented-out block would enable.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browden.configs.loader import AllowlistRefresher
from browden.mcp.validator import ActionAllowlist, ValidationError

# Mirror of allowlist.yaml with the showcase click block uncommented.
_ENABLED_ALLOWLIST = ActionAllowlist({
    "read": {"website_overrides": {"*": [".*"]}},
    "click": {
        "amazon.com": {"paths": [".*"], "label": r"(?i)\badd to cart\b"},
    },
})

# Same, but amazon.com is also on the denylist — the denylist must win.
_DENIED_ALLOWLIST = ActionAllowlist({
    "denylist": {"amazon.com": [".*"]},
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
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/B0FBRRM2VQ", elements=[_atc_node()])
    with patch.object(server._store, "route", return_value=session):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.click("#add-to-cart-button", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_happy_path_clicks():
    import browden.mcp.server as server
    importlib = __import__("importlib")
    importlib.reload(server)
    session = _session(url="https://www.amazon.com/dp/B0FBRRM2VQ", elements=[_atc_node()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ENABLED_ALLOWLIST))):
        result = await server.click("#add-to-cart-button", "h1")
    assert result["clicked"] is True
    session.click.assert_awaited_once_with("#add-to-cart-button", id="h1")


@pytest.mark.asyncio
async def test_denylist_vetoes_click_even_when_click_host_is_allowed():
    # amazon.com is on the click allowlist AND the denylist — denylist wins, so
    # the element is never even inspected.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/B0FBRRM2VQ", elements=[_atc_node()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_DENIED_ALLOWLIST))):
        with pytest.raises(ValidationError, match="denylist"):
            await server.click("#add-to-cart-button", "h1")
    session.query_selector_all.assert_not_awaited()
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_not_allowed_is_rejected():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://evil.example.com/p", elements=[_atc_node()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ENABLED_ALLOWLIST))):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.click("#x", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_buy_now_rejected_by_site_label():
    # "Buy Now" is a real control (the predicate no longer vetoes it on intent),
    # but amazon.com requires the "add to cart" label — so Gate 3 refuses it.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/X",
                       elements=[_atc_node(value="Buy Now")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ENABLED_ALLOWLIST))):
        with pytest.raises(ValidationError, match="required label"):
            await server.click("#buy-now", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_allow_all_host_clicks_any_real_control():
    # A host listed with paths but NO label means "any click here is fine" — so a
    # "Place your order" button (once vetoed by the hardcoded negative list) now
    # clicks. Integrity still holds: it must be a real, non-decoy control.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    allow_all = ActionAllowlist({
        "read": {"website_overrides": {"*": [".*"]}},
        "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},  # explicit allow-any
    })
    session = _session(url="https://www.amazon.com/cart",
                       elements=[{"tag": "button", "id": None, "classes": [],
                                  "attributes": {}, "text": "Place your order"}])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(allow_all))):
        result = await server.click("#place-order", "h1")
    assert result["clicked"] is True
    session.click.assert_awaited_once_with("#place-order", id="h1")


@pytest.mark.asyncio
async def test_allow_all_host_still_rejects_decoy():
    # "Any click" does not extend to page-injected agent decoys — that guard is
    # intent-independent and always applies.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    allow_all = ActionAllowlist({
        "read": {"website_overrides": {"*": [".*"]}},
        "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},
    })
    session = _session(url="https://www.amazon.com/cart",
                       elements=[_atc_node(value="Place your order",
                                           **{"data-target-audience": "ai-agent"})])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(allow_all))):
        with pytest.raises(ValidationError, match="decoy"):
            await server.click("#decoy", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_selector_is_rejected():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/X",
                       elements=[_atc_node(), _atc_node()])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ENABLED_ALLOWLIST))):
        with pytest.raises(ValidationError, match="ambiguous"):
            await server.click(".a-button-input", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_mismatch_for_site_is_rejected():
    # "Add to bag" passes the generic predicate but amazon.com requires "add to cart".
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/dp/X",
                       elements=[_atc_node(value="Add to bag")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ENABLED_ALLOWLIST))):
        with pytest.raises(ValidationError, match="required label"):
            await server.click("#x", "h1")
    session.click.assert_not_awaited()


# Anchor clicks are gated by *where the href goes* against the READ allowlist.
# Here amazon.com and wholefoodsmarket.com are readable; nothing else is.
_ANCHOR_ALLOWLIST = ActionAllowlist({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"amazon.com": [".*"], "wholefoodsmarket.com": [".*"]}},
    "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},  # allow any control text
})


def _anchor_node(href, text="link", **attrs):
    return {"tag": "a", "id": None, "classes": [],
            "attributes": {"href": href, **attrs}, "text": text}


@pytest.mark.asyncio
async def test_anchor_same_site_relative_clicks():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout/p/x/spc",
                       elements=[_anchor_node("/checkout/next")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ANCHOR_ALLOWLIST))):
        result = await server.click("a.next", "h1")
    assert result["clicked"] is True


@pytest.mark.asyncio
async def test_anchor_cross_domain_but_allowlisted_clicks():
    # The whole point of the read-allowlist rule (vs same-domain): an anchor that
    # leaves amazon.com for another ALLOW-LISTED site is fine.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout/p/x/spc",
                       elements=[_anchor_node("https://www.wholefoodsmarket.com/cart")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ANCHOR_ALLOWLIST))):
        result = await server.click("a.wf", "h1")
    assert result["clicked"] is True


@pytest.mark.asyncio
async def test_anchor_target_off_read_allowlist_rejected():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout/p/x/spc",
                       elements=[_anchor_node("https://evil.example/x")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ANCHOR_ALLOWLIST))):
        with pytest.raises(ValidationError, match="not on the read allowlist"):
            await server.click("a.evil", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_anchor_javascript_href_clicks_without_target_check():
    # javascript:void(0) runs in place (the tip "Edit" pattern) — no navigation,
    # so no read-allowlist check; it clicks even with no target host.
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout/p/x/spc",
                       elements=[_anchor_node("javascript:void(0)", text="Edit")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ANCHOR_ALLOWLIST))):
        result = await server.click("a.edit", "h1")
    assert result["clicked"] is True


@pytest.mark.asyncio
async def test_anchor_mailto_scheme_rejected():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url="https://www.amazon.com/checkout/p/x/spc",
                       elements=[_anchor_node("mailto:help@amazon.com", text="Contact")])
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_default_state", server.AppState(AllowlistRefresher.static(_ANCHOR_ALLOWLIST))):
        with pytest.raises(ValidationError, match="non-navigational scheme"):
            await server.click("a.mail", "h1")
    session.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_gone_returns_error():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    session = _session(url=None, elements=[])
    with patch.object(server._store, "route", return_value=session):
        result = await server.click("#x", "h1")
    assert "error" in result and result["id"] == "h1"
    session.click.assert_not_awaited()
