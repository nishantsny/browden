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
    validate_click_target,
    validate_press_key_target,
    validate_write_text_target,
)

# amazon.com readable + click/write-text enabled; nothing else is.
_ALLOWLIST = ActionAllowlist({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"amazon.com": [".*"], "wholefoodsmarket.com": [".*"]}},
    "click": {"amazon.com": {"paths": [".*"], "label": r"(?i)\badd to cart\b"}},
    "write-text": {"amazon.com": {"paths": [".*"], "label": r"(?i)grocery tip.*",
                                  "field_ids": ["tip-amount"]}},
}).policy

_DENIED = ActionAllowlist({
    "denylist": {"amazon.com": [".*"]},
    "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},
}).policy

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
    with pytest.raises(ValidationError, match="no click rule authorizes"):
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
    with pytest.raises(ValidationError, match="does not match any click label"):
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
    }).policy
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
    with pytest.raises(ValidationError, match="does not match any write-text rule"):
        validate_write_text_target(_ALLOWLIST, AMAZON, "#x", _found(_field(label="Coupon code")))

# -- validate_press_key_target (Gates 2, 2b, 3) ------------------------------

# cronometer.com press-key enabled: Enter + up/down arrows on the app path '/'.
_PK = ActionAllowlist({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"cronometer.com": [".*"]}},
    "press-key": {"cronometer.com": [
        {"path": ["^/$"], "label": ".*", "keys": ["Enter", "ArrowDown", "ArrowUp"]},
    ]},
}).policy
# same, but the rule only admits rows whose text starts with "Fried".
_PK_LABELLED = ActionAllowlist({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"cronometer.com": [".*"]}},
    "press-key": {"cronometer.com": [
        {"path": ["^/$"], "label": r"(?i)fried.*", "keys": ["Enter"]},
    ]},
}).policy
CRONO = "https://cronometer.com/"


def _row(text="Fried Eggs, Whole Egg", tag="tr", **attrs):
    return {"tag": tag, "id": None, "classes": [],
            "attributes": {"tabindex": "0", **attrs}, "text": text}


def test_press_key_host_not_on_section_rejected():
    with pytest.raises(ValidationError, match="no press-key rule authorizes"):
        check_action_host(_PK, "press-key", "https://evil.example.com/p")


def test_press_key_happy_path_enter():
    validate_press_key_target(_PK, CRONO, "tr", _found(_row()), "Enter")  # no raise


def test_press_key_arrow_navigation_allowed():
    validate_press_key_target(_PK, CRONO, "tr", _found(_row()), "ArrowDown")  # no raise


def test_press_key_non_focusable_rejected():
    div = {"tag": "div", "id": None, "classes": [], "attributes": {}, "text": "row"}
    with pytest.raises(ValidationError, match="not a focusable control"):
        validate_press_key_target(_PK, CRONO, "div", _found(div), "Enter")


def test_press_key_character_key_rejected():
    # A character key is never allowed — that's write-text's job.
    with pytest.raises(ValidationError, match="not an allowed control key"):
        validate_press_key_target(_PK, CRONO, "tr", _found(_row()), "a")


def test_press_key_unlisted_control_key_rejected():
    # Escape is a valid control key, but this rule only authorizes Enter/arrows.
    with pytest.raises(ValidationError, match="no press-key rule authorizes"):
        validate_press_key_target(_PK, CRONO, "tr", _found(_row()), "Escape")


def test_press_key_label_mismatch_rejected():
    # The labelled rule only admits rows starting with "Fried".
    validate_press_key_target(_PK_LABELLED, CRONO, "tr", _found(_row("Fried Eggs")), "Enter")
    with pytest.raises(ValidationError, match="no press-key rule authorizes"):
        validate_press_key_target(_PK_LABELLED, CRONO, "tr", _found(_row("Avocado, raw")), "Enter")


def test_press_key_decoy_rejected():
    node = _row(**{"data-target-audience": "ai-agent"})
    with pytest.raises(ValidationError, match="decoy"):
        validate_press_key_target(_PK, CRONO, "tr", _found(node), "Enter")


def test_press_key_ambiguous_rejected():
    with pytest.raises(ValidationError, match="ambiguous"):
        validate_press_key_target(_PK, CRONO, "tr", _found(_row(), _row()), "Enter")


# The read-tool gate (is_url_allowed / ensure_url_is_in_allowlist) now lives in
# read_gates.py and is covered by test_read_gates.py.


# -- allow_all: the write gates, through a scratch profile's rule set (#139) ---

_SCRATCH = ActionAllowlist({"profiles": {"/profiles/scratch": {
    "allow_all": True, "read": {"tranco": {"enabled": False}}}}}).policy_for("/profiles/scratch")


def test_allow_all_authorizes_a_click_through_the_real_gates():
    node = {"tag": "button", "id": None, "classes": [], "attributes": {},
            "text": "Anything at all"}
    check_action_host(_SCRATCH, "click", "https://unlisted.test/whatever")
    validate_click_target(_SCRATCH, "https://unlisted.test/whatever", "#b", _found(node))


def test_allow_all_authorizes_typing_into_a_field_with_no_visible_label():
    # A label-less box is the case an operator normally has to name by id; under
    # allow_all the "any label" rule covers it without one.
    node = {"tag": "input", "id": "x", "classes": [], "attributes": {"type": "text"},
            "text": ""}
    check_action_host(_SCRATCH, "write-text", "https://unlisted.test/form")
    validate_write_text_target(_SCRATCH, "https://unlisted.test/form", "#x", _found(node))


def test_allow_all_authorizes_a_control_key_but_never_a_character_key():
    node = {"tag": "tr", "id": None, "classes": [], "attributes": {"tabindex": "0"},
            "text": "A row"}
    validate_press_key_target(_SCRATCH, "https://unlisted.test/list", "tr", _found(node), "Enter")
    with pytest.raises(ValidationError, match="not an allowed control key"):
        validate_press_key_target(_SCRATCH, "https://unlisted.test/list", "tr", _found(node), "a")


def test_allow_all_does_not_authorize_a_click_on_a_denied_host():
    denied = ActionAllowlist({
        "denylist": {"blocked.test": [".*"]},
        "profiles": {"/profiles/scratch": {"allow_all": True}},
    }).policy_for("/profiles/scratch")
    with pytest.raises(ValidationError, match="denylist"):
        check_action_host(denied, "click", "https://blocked.test/x")


def test_allow_all_still_gates_where_an_anchor_would_navigate():
    # The anchor check runs against the same read policy, so a link off to an
    # unranked host is refused even in an allow_all profile with Tranco on.
    scratch = ActionAllowlist({"profiles": {"/profiles/s": {"allow_all": True}}}).policy_for("/profiles/s")
    anchor = {"tag": "a", "id": None, "classes": [],
              "attributes": {"href": "https://unranked.test/x"}, "text": "Go"}
    with pytest.raises(ValidationError, match="not on the read allowlist"):
        validate_click_target(scratch, "https://google.com/", "a", _found(anchor))
