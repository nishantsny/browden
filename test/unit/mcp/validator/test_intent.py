import re

import pytest

from browser_guard.mcp.validator import is_add_to_cart, label_matches


def node(tag="input", *, text="", **attrs):
    """Build a serialized-element dict like dom.serialize.element_to_node returns."""
    el_id = attrs.pop("id", None)
    classes = attrs.pop("classes", [])
    return {"tag": tag, "id": el_id, "classes": classes, "attributes": attrs, "text": text}


# -- the real Amazon add-to-cart button (from the live tab) -----------------

AMAZON_ATC = node(
    "input",
    type="submit",
    name="submit.add-to-cart",
    title="Add to Shopping Cart",
    value="Add to cart",
)


def test_amazon_real_button_accepted():
    assert is_add_to_cart(AMAZON_ATC)


def test_button_tag_with_text_accepted():
    assert is_add_to_cart(node("button", text="Add to Cart"))


def test_aria_label_accepted():
    assert is_add_to_cart(node("button", text="", **{"aria-label": "Add to bag"}))


def test_role_button_accepted():
    assert is_add_to_cart(node("a", text="Add to basket", role="button"))


# -- negatives: must never be clickable --------------------------------------

def test_buy_now_rejected():
    assert not is_add_to_cart(node("input", type="submit", value="Buy Now"))


def test_one_click_rejected():
    assert not is_add_to_cart(node("input", type="submit", value="Buy with 1-Click"))


def test_subscribe_rejected():
    assert not is_add_to_cart(node("button", text="Subscribe & Save"))


def test_remove_and_wishlist_rejected():
    assert not is_add_to_cart(node("button", text="Remove from cart"))
    assert not is_add_to_cart(node("button", text="Add to Wish List"))


def test_checkout_rejected():
    assert not is_add_to_cart(node("button", text="Proceed to checkout"))


def test_unrelated_text_rejected():
    assert not is_add_to_cart(node("button", text="Add to comparison"))
    assert not is_add_to_cart(node("input", type="text", value="Add to cart"))  # not a button


def test_hidden_or_disabled_rejected():
    assert not is_add_to_cart(node("button", text="Add to cart", **{"aria-hidden": "true"}))
    assert not is_add_to_cart(node("button", text="Add to cart", disabled=""))
    assert not is_add_to_cart(node("input", type="hidden", value="Add to cart"))


def test_agent_decoy_rejected():
    # A tab-supplied control marked "for AI agents" must never be trusted, even
    # if its text says "Add to cart".
    assert not is_add_to_cart(
        node("input", type="submit", value="Add to cart",
             **{"data-target-audience": "ai-agent", "data-agent-recommended": "true"}))
    assert not is_add_to_cart(
        node("button", text="Add to cart", **{"data-agent-action": "primary-search"}))


def test_none_rejected():
    assert not is_add_to_cart(None)
    assert not is_add_to_cart({})


# -- per-site label enforcement ----------------------------------------------

def test_label_matches_uses_visible_labels():
    pat = re.compile(r"(?i)\badd to cart\b")
    assert label_matches(AMAZON_ATC, pat)                      # via value=
    assert label_matches(node("button", text="ADD TO CART"), pat)
    assert not label_matches(node("button", text="Add to bag"), pat)
    assert not label_matches(None, pat)
