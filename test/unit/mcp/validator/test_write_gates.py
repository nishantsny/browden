"""Tests for the composed write-action gate sequences.

These exercise the gate functions moved out of ``server.py`` directly — no
session or FastMCP in the picture, just the allowlist + a serialized element
node. The tool-level tests (``test_server_click`` / ``test_server_insert_text``)
still cover the same gates end-to-end; this file pins the composition unit.
"""
import pytest

from browden.mcp.validator import (
    ActionAllowlist,
    ValidationError,
    check_action_host,
    ensure_url_is_in_allowlist,
    is_url_allowed,
    validate_click_target,
    validate_write_text_target,
)

# amazon.com readable + click/write-text enabled; nothing else is.
_ALLOWLIST = ActionAllowlist({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"amazon.com": [".*"], "wholefoodsmarket.com": [".*"]}},
    "click": {"amazon.com": {"paths": [".*"], "label": r"(?i)\badd to cart\b"}},
    "write-text": {"amazon.com": {"paths": [".*"], "label": r"(?i)grocery tip.*",
                                  "field_ids": ["tip-amount"]}},
})

_DENIED = ActionAllowlist({
    "denylist": {"amazon.com": [".*"]},
    "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},
})

AMAZON = "https://www.amazon.com/dp/B0FBRRM2VQ"


def _found(*nodes):
    return {"total_count": len(nodes), "elements": list(nodes)}


def _atc(value="Add to cart", text="", **attrs):
    return {"tag": "input", "id": None, "classes": [],
            "attributes": {"type": "submit", "value": value, **attrs}, "text": text}


def _anchor(href, text="link", **attrs):
    return {"tag": "a", "id": None, "classes": [],
            "attributes": {"href": href, **attrs}, "text": text}


def _field(label="Grocery Tip", node_id=None, **attrs):
    return {"tag": "input", "id": node_id, "classes": [],
            "attributes": {"type": "text", "aria-label": label, **attrs}, "text": ""}


# -- check_action_host (Gate 1) ----------------------------------------------

def test_host_allowed_passes():
    check_action_host(_ALLOWLIST, "click", AMAZON)  # no raise


def test_host_not_on_section_rejected():
    with pytest.raises(ValidationError, match="not on allowlist"):
        check_action_host(_ALLOWLIST, "click", "https://evil.example.com/p")


def test_denylist_vetoes_even_allowed_host():
    with pytest.raises(ValidationError, match="denylist"):
        check_action_host(_DENIED, "click", AMAZON)


# -- validate_click_target (Gates 2, 2b, 3) ----------------------------------

def test_click_happy_path():
    validate_click_target(_ALLOWLIST, AMAZON, "#atc", _found(_atc()))  # no raise


def test_click_no_match_rejected():
    with pytest.raises(ValidationError, match="no element matches"):
        validate_click_target(_ALLOWLIST, AMAZON, "#x", _found())


def test_click_ambiguous_rejected():
    with pytest.raises(ValidationError, match="ambiguous"):
        validate_click_target(_ALLOWLIST, AMAZON, ".a", _found(_atc(), _atc()))


def test_click_decoy_rejected():
    node = _atc(**{"data-target-audience": "ai-agent"})
    with pytest.raises(ValidationError, match="decoy"):
        validate_click_target(_ALLOWLIST, AMAZON, "#d", _found(node))


def test_click_label_mismatch_rejected():
    with pytest.raises(ValidationError, match="required label"):
        validate_click_target(_ALLOWLIST, AMAZON, "#x", _found(_atc(value="Add to bag")))


def test_click_anchor_off_read_allowlist_rejected():
    node = _anchor("https://evil.example/x")
    with pytest.raises(ValidationError, match="not on the read allowlist"):
        validate_click_target(_ALLOWLIST, AMAZON, "a.evil", _found(node))


def test_click_anchor_cross_domain_allowlisted_passes():
    node = _anchor("https://www.wholefoodsmarket.com/cart")
    # cross-domain but the target is read-allowed, and click label is '(?i)add to
    # cart' — anchor text won't match, so use the allow-any host for this one.
    allow_any = ActionAllowlist({
        "read": {"enabled": True, "tranco": {"enabled": False},
                 "website_overrides": {"amazon.com": [".*"], "wholefoodsmarket.com": [".*"]}},
        "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},
    })
    validate_click_target(allow_any, AMAZON, "a.wf", _found(node))  # no raise


def test_click_anchor_mailto_rejected():
    node = _anchor("mailto:help@amazon.com", text="Contact")
    with pytest.raises(ValidationError, match="non-navigational scheme"):
        validate_click_target(_ALLOWLIST, AMAZON, "a.mail", _found(node))


# -- validate_write_text_target (Gates 2, 3) ---------------------------------

def test_write_text_happy_path_by_label():
    validate_write_text_target(_ALLOWLIST, AMAZON, "#tip", _found(_field()))  # no raise


def test_write_text_by_field_id_escape_hatch():
    # A label-less box authorized by its exact id/name in field_ids.
    node = _field(label="", node_id="tip-amount")
    validate_write_text_target(_ALLOWLIST, AMAZON, "#tip", _found(node))  # no raise


def test_write_text_non_text_control_rejected():
    button = {"tag": "button", "id": None, "classes": [], "attributes": {}, "text": "Go"}
    with pytest.raises(ValidationError, match="fillable text control"):
        validate_write_text_target(_ALLOWLIST, AMAZON, "#b", _found(button))


def test_write_text_label_mismatch_rejected():
    with pytest.raises(ValidationError, match="required write-text label"):
        validate_write_text_target(_ALLOWLIST, AMAZON, "#x", _found(_field(label="Coupon code")))


# -- read gate: is_url_allowed / ensure_url_is_in_allowlist (H2) --------------

def test_is_url_allowed_reflects_read_policy():
    assert is_url_allowed(_ALLOWLIST, AMAZON)                       # amazon override
    assert not is_url_allowed(_ALLOWLIST, "https://evil.example.com/x")


def test_ensure_url_is_in_allowlist_passes_for_allowed_host():
    ensure_url_is_in_allowlist(_ALLOWLIST, AMAZON)  # no raise


def test_ensure_url_is_in_allowlist_raises_for_denied_host():
    with pytest.raises(ValidationError, match="read allowlist"):
        ensure_url_is_in_allowlist(_ALLOWLIST, "https://evil.example.com/x")


def test_ensure_url_is_in_allowlist_respects_denylist():
    # _DENIED denylists amazon.com — a denied host is never read-allowed either.
    assert not is_url_allowed(_DENIED, AMAZON)
    with pytest.raises(ValidationError):
        ensure_url_is_in_allowlist(_DENIED, AMAZON)


def test_read_gate_exempts_non_web_schemes():
    # about:blank / new-tab / data: have no web host the read allowlist governs,
    # so a read tool never refuses them (the agent can't navigate there anyway).
    for u in ("about:blank", "data:text/html,<h1>hi</h1>", "chrome://newtab/"):
        assert is_url_allowed(_ALLOWLIST, u)
        ensure_url_is_in_allowlist(_ALLOWLIST, u)  # no raise
