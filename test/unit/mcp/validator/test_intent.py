import re

import pytest

from browser_guard.mcp.validator import is_clickable_control, label_matches


def node(tag="input", *, text="", **attrs):
    """Build a serialized-element dict like dom.serialize.element_to_node returns."""
    el_id = attrs.pop("id", None)
    classes = attrs.pop("classes", [])
    return {"tag": tag, "id": el_id, "classes": classes, "attributes": attrs, "text": text}


# -- accepted: real, visible, non-decoy clickable controls -------------------

AMAZON_ATC = node(
    "input",
    type="submit",
    name="submit.add-to-cart",
    title="Add to Shopping Cart",
    value="Add to cart",
)


def test_amazon_real_button_accepted():
    assert is_clickable_control(AMAZON_ATC)


def test_button_tag_with_text_accepted():
    assert is_clickable_control(node("button", text="Add to Cart"))


def test_aria_label_accepted():
    assert is_clickable_control(node("button", text="", **{"aria-label": "Add to bag"}))


def test_role_button_accepted():
    assert is_clickable_control(node("a", text="Add to basket", role="button"))


def test_quantity_option_button_accepted():
    # A quantity picker option is a real button — is_clickable_control no longer
    # cares that its text isn't "add to cart"; the operator's label gates that.
    assert is_clickable_control(node("button", text="8 est. 3.44 lb", role="option"))


# -- intent is NOT judged here: what a control DOES is the operator's call ----

@pytest.mark.parametrize("label", [
    "Buy Now", "Buy with 1-Click", "Subscribe & Save", "Remove from cart",
    "Add to Wish List", "Proceed to checkout", "Place your order", "Add to comparison",
])
def test_real_controls_pass_regardless_of_action(label):
    # These were once rejected by a hardcoded negative list. Intent is now the
    # operator's decision (per-host label), so the integrity check lets them
    # through — whether they may be clicked is decided by the allowlist label.
    assert is_clickable_control(node("button", text=label))


# -- still rejected: integrity failures (not intent) -------------------------

def test_non_clickable_element_rejected():
    assert not is_clickable_control(node("input", type="text", value="Add to cart"))
    assert not is_clickable_control(node("div", text="Add to cart"))
    assert not is_clickable_control(node("span", text="Add to cart"))


def test_hidden_or_disabled_rejected():
    assert not is_clickable_control(node("button", text="Add to cart", **{"aria-hidden": "true"}))
    assert not is_clickable_control(node("button", text="Add to cart", disabled=""))
    assert not is_clickable_control(node("input", type="hidden", value="Add to cart"))


def test_agent_decoy_rejected():
    # A tab-supplied control marked "for AI agents" must never be trusted, even
    # if its text says "Add to cart" — this defends against the page, and is the
    # one intent-independent guard that stays.
    assert not is_clickable_control(
        node("input", type="submit", value="Add to cart",
             **{"data-target-audience": "ai-agent", "data-agent-recommended": "true"}))
    assert not is_clickable_control(
        node("button", text="Add to cart", **{"data-agent-action": "primary-search"}))


def test_none_rejected():
    assert not is_clickable_control(None)
    assert not is_clickable_control({})


# -- per-site label enforcement ----------------------------------------------

def test_label_matches_uses_visible_labels():
    pat = re.compile(r"(?i)\badd to cart\b")
    assert label_matches(AMAZON_ATC, pat)                      # via value="Add to cart"
    assert label_matches(node("button", text="ADD TO CART"), pat)
    assert not label_matches(node("button", text="Add to bag"), pat)
    assert not label_matches(None, pat)


def test_label_matches_requires_full_match_not_substring():
    # The pattern must match the ENTIRE label, not merely appear within it — so a
    # loose regex can't wave through a control that only *contains* "add to cart".
    pat = re.compile(r"(?i)add to cart")
    # Full match, but still case-insensitive (the (?i) flag survives fullmatch):
    for casing in ("Add to Cart", "add to cart", "ADD TO CART"):
        assert label_matches(node("button", text=casing), pat)
    assert not label_matches(node("button", text="Add to cart bundle"), pat)  # trailing text
    assert not label_matches(node("button", text="Please Add to cart"), pat)  # leading text
    # An operator who *wants* a trailing item name must say so explicitly.
    wfm = re.compile(r"(?i)add to cart\b.*")
    assert label_matches(node("button", **{"aria-label": "Add to Cart, gala apple"}), wfm)
