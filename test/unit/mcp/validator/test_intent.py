import re

import pytest

from browden.mcp.validator import (
    ACTIVATION_KEYS,
    classify_anchor_target,
    field_label_matches,
    is_clickable_control,
    is_fillable_control,
    is_label_activation,
    is_focusable_control,
    label_matches,
)


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


def test_label_matches_uses_aria_labelledby_text():
    # A label-less submit (no text/value/aria-label) whose visible name the
    # serializer resolved from aria-labelledby into node["labelledby_text"].
    # This is the Amazon "a-button" checkout/continue button.
    btn = node("input", type="submit")
    btn["labelledby_text"] = "Continue"
    assert label_matches(btn, re.compile(r"(?i)continue"))
    assert not label_matches(btn, re.compile(r"(?i)add to cart"))
    # And it still fully governs: a substring-only regex must not pass.
    btn["labelledby_text"] = "Continue to payment"
    assert not label_matches(btn, re.compile(r"(?i)continue"))


# -- insert_text (write-text): integrity of a text control --------------------------

def test_text_inputs_and_textarea_and_contenteditable_are_fillable():
    assert is_fillable_control(node("input", type="text"))
    assert is_fillable_control(node("input", type="number"))
    assert is_fillable_control(node("input"))          # default type is text
    assert is_fillable_control(node("textarea"))
    assert is_fillable_control(node("div", **{"contenteditable": ""}))
    assert is_fillable_control(node("div", **{"contenteditable": "true"}))


def test_non_text_controls_are_not_fillable():
    for t in ("checkbox", "radio", "file", "range", "color", "submit", "button"):
        assert not is_fillable_control(node("input", type=t)), t
    assert not is_fillable_control(node("button", text="Add to cart"))
    assert not is_fillable_control(node("div", text="hi"))
    assert not is_fillable_control(node("div", **{"contenteditable": "false"}))
    assert not is_fillable_control(None)
    assert not is_fillable_control({})


def test_hidden_disabled_readonly_decoy_not_fillable():
    assert not is_fillable_control(node("input", type="text", disabled=""))
    assert not is_fillable_control(node("input", type="text", readonly=""))
    assert not is_fillable_control(node("input", type="text", **{"aria-readonly": "true"}))
    assert not is_fillable_control(node("input", type="text", **{"aria-hidden": "true"}))
    assert not is_fillable_control(node("input", type="hidden"))
    # page-injected agent decoy is never a trustworthy target, even as a text box
    assert not is_fillable_control(
        node("input", type="text", **{"data-target-audience": "ai-agent"}))


# -- insert_text (write-text): the field's visible label is what gets matched --------

def test_field_label_matches_visible_field_names():
    pat = re.compile(r"(?i)grocery tip.*")
    assert field_label_matches(node("input", type="number", placeholder="Grocery Tip (optional)"), pat)
    assert field_label_matches(node("input", type="number", **{"aria-label": "Grocery tip amount"}), pat)
    assert field_label_matches(node("input", type="number", title="Grocery tip"), pat)
    # resolved by the serializer from aria-labelledby / <label>:
    from_lblby = node("input", type="number")
    from_lblby["labelledby_text"] = "Grocery Tip"
    assert field_label_matches(from_lblby, pat)
    from_label = node("input", type="number")
    from_label["field_label"] = "Grocery Tip (optional):"
    assert field_label_matches(from_label, pat)


def test_field_label_no_visible_name_does_not_match():
    # The Amazon tip <input> has no placeholder/aria-label/aria-labelledby/<label>,
    # so there is nothing for the write-text regex to match — the guard refuses.
    assert not field_label_matches(node("input", type="number"), re.compile(r"(?i)grocery tip.*"))
    # value/name/id are NOT candidates (not user-visible)
    assert not field_label_matches(
        node("input", type="number", value="5.00", name="updateAmountInput"),
        re.compile(r"(?i)updateAmountInput"))


def test_field_label_requires_full_match_not_substring():
    assert not field_label_matches(
        node("input", placeholder="Monthly grocery tip for the driver"),
        re.compile(r"(?i)grocery tip"))
    assert field_label_matches(
        node("input", placeholder="Monthly grocery tip for the driver"),
        re.compile(r"(?i).*grocery tip.*"))


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


# -- anchors: accepted as controls, classified for the read-allowlist gate ----

def test_anchor_accepted_as_clickable_control():
    # A plain <a> is a real clickable control now (its *target* is gated
    # elsewhere, not here); decoy/hidden anchors are still rejected.
    assert is_clickable_control(node("a", text="Edit", href="javascript:void(0)"))
    assert is_clickable_control(node("a", text="Delivery", href="/checkout/next"))
    assert not is_clickable_control(node("a", text="Edit", href="/x", **{"aria-hidden": "true"}))
    assert not is_clickable_control(node("a", text="Edit", href="/x", **{"data-agent-action": "go"}))


CUR = "https://www.amazon.com/checkout/p/x/spc"


def test_classify_non_anchor_is_inpage():
    # Buttons/inputs have no href to leave by — always in-page (no target check).
    assert classify_anchor_target(node("button", text="Apply"), CUR) == ("inpage", None)
    assert classify_anchor_target(None, CUR) == ("inpage", None)


@pytest.mark.parametrize("href", ["javascript:void(0)", "", None])
def test_classify_inpage_hrefs(href):
    # javascript: handlers and empty/absent hrefs run in place — no navigation.
    attrs = {} if href is None else {"href": href}
    assert classify_anchor_target(node("a", text="Edit", **attrs), CUR) == ("inpage", None)


def test_classify_relative_and_fragment_resolve_to_current_site():
    kind, target = classify_anchor_target(node("a", text="Next", href="/checkout/next"), CUR)
    assert kind == "nav" and target == "https://www.amazon.com/checkout/next"
    kind, target = classify_anchor_target(node("a", text="Jump", href="#tips"), CUR)
    assert kind == "nav" and target.startswith("https://www.amazon.com/checkout/p/x/spc#tips")


def test_classify_cross_domain_is_nav_not_blocked():
    # Cross-domain is NOT rejected here — it returns ("nav", url); whether it is
    # allowed is the caller's read-allowlist decision (not a same-domain test).
    kind, target = classify_anchor_target(node("a", text="Go", href="https://evil.example/x"), CUR)
    assert kind == "nav" and target == "https://evil.example/x"
    # Protocol-relative resolves to the current scheme + the other host.
    kind, target = classify_anchor_target(node("a", text="Go", href="//other.example/y"), CUR)
    assert kind == "nav" and target == "https://other.example/y"


@pytest.mark.parametrize("href", ["mailto:a@b.com", "tel:+15551234", "data:text/html,hi", "file:///etc/passwd"])
def test_classify_non_navigational_schemes_blocked(href):
    assert classify_anchor_target(node("a", text="x", href=href), CUR) == ("blocked", None)


# -- press-key: integrity of a FOCUSABLE control -----------------------------

def test_natively_focusable_tags_are_focusable():
    for tag in ("input", "button", "select", "textarea"):
        assert is_focusable_control(node(tag)), tag


def test_anchor_focusable_only_with_href():
    assert is_focusable_control(node("a", text="Home", href="/"))
    assert not is_focusable_control(node("a", text="Home"))   # no href → not focusable


def test_tabindex_makes_generic_element_focusable():
    # The motivating case: a food-search result row is a <tr tabindex="0"> the
    # click gate refuses, but which IS keyboard-focusable.
    assert is_focusable_control(node("tr", text="Fried Eggs, Whole Egg", tabindex="0"))
    assert is_focusable_control(node("div", text="option", tabindex="-1"))
    assert is_focusable_control(node("li", text="row", tabindex="0", role="option"))


def test_generic_element_without_tabindex_not_focusable():
    # A bare <div onclick> that a coordinate click could hit is NOT focusable — the
    # whole point of the narrower press-key reach.
    assert not is_focusable_control(node("div", text="click me"))
    assert not is_focusable_control(node("tr", text="row"))
    assert not is_focusable_control(node("span", text="x", onclick="go()"))


def test_focusable_still_rejects_integrity_failures():
    # tabindex doesn't rescue a hidden/disabled/decoy element.
    assert not is_focusable_control(node("tr", text="row", tabindex="0", **{"aria-hidden": "true"}))
    assert not is_focusable_control(node("input", disabled=""))
    assert not is_focusable_control(node("div", tabindex="0", **{"display:none": ""}, style="display:none"))
    assert not is_focusable_control(
        node("tr", text="row", tabindex="0", **{"data-target-audience": "ai-agent"}))
    assert not is_focusable_control(None)
    assert not is_focusable_control({})


def test_activation_keys_are_control_keys_only():
    # The universe press-key may ever send: activation/navigation, never characters.
    assert "Enter" in ACTIVATION_KEYS and "ArrowDown" in ACTIVATION_KEYS
    assert "Escape" in ACTIVATION_KEYS and "Tab" in ACTIVATION_KEYS
    for ch in ("a", "A", "1", " ", "x", "Delete", "Backspace"):
        assert ch not in ACTIVATION_KEYS, ch


# -- <label> activation of CSS-hidden radios / checkboxes (issue #146) --------
#
# The pattern: the page hides the native input and draws the visible control as a
# ::before on its <label>. The label is then the only affordance a human — or the
# agent — can click.

def label_node(text="After submitting the physical application", *,
               control_tag="input", control_type="radio", **control_attrs):
    """A serialized <label> node carrying the control it activates."""
    attrs = {"type": control_type} if control_type is not None else {}
    attrs.update(control_attrs)
    return {
        "tag": "label", "id": None, "classes": [], "attributes": {}, "text": text,
        "labeled_control": {"tag": control_tag, "attributes": attrs},
    }


@pytest.mark.parametrize("control_type", ["radio", "checkbox"])
def test_label_bound_to_radio_or_checkbox_accepted(control_type):
    assert is_label_activation(label_node(control_type=control_type))
    assert is_clickable_control(label_node(control_type=control_type))


def test_label_activation_survives_the_hidden_input_it_points_at():
    """The whole point: the control is hidden, and that must not disqualify it."""
    assert is_clickable_control(label_node(style="display: none"))
    assert is_clickable_control(label_node(style="position:absolute;left:-9999px"))


def test_label_with_no_associated_control_rejected():
    bare = {"tag": "label", "id": None, "classes": [], "attributes": {}, "text": "Just text"}
    assert not is_label_activation(bare)
    assert not is_clickable_control(bare)


def test_label_bound_to_text_field_rejected():
    """Clicking it only moves focus — there is nothing to authorize."""
    assert not is_clickable_control(label_node(control_type="text"))


def test_label_bound_to_submit_button_rejected():
    """A label's text must never stand in for a submit button's own gated text."""
    assert not is_clickable_control(label_node(control_type="submit"))
    assert not is_clickable_control(label_node(control_type="button"))


def test_label_bound_to_select_or_textarea_rejected():
    assert not is_clickable_control(label_node(control_tag="select", control_type=None))
    assert not is_clickable_control(label_node(control_tag="textarea", control_type=None))


def test_label_bound_to_disabled_control_rejected():
    assert not is_clickable_control(label_node(disabled=""))
    assert not is_clickable_control(label_node(**{"aria-disabled": "true"}))


def test_label_bound_to_decoy_control_rejected():
    """The page must not smuggle an agent decoy in through the label's target."""
    assert not is_clickable_control(label_node(**{"data-agent-action": "confirm"}))
    assert not is_clickable_control(label_node(**{"data-target-audience": "ai-agent"}))


def test_label_bound_to_type_hidden_or_hidden_attr_rejected():
    """Inert in a way display:none is not — nothing to toggle."""
    assert not is_clickable_control(label_node(control_type="hidden"))
    assert not is_clickable_control(label_node(hidden=""))
    assert not is_clickable_control(label_node(**{"aria-hidden": "true"}))


def test_hidden_or_decoy_label_itself_still_rejected():
    """The relaxation applies to the target, never to the label a human must see."""
    n = label_node()
    n["attributes"] = {"style": "display:none"}
    assert not is_clickable_control(n)
    n2 = label_node()
    n2["attributes"] = {"data-target-audience": "ai-agent"}
    assert not is_clickable_control(n2)


def test_label_activation_still_gated_on_the_labels_visible_text():
    """Gate 3 matches the label's own text — the string the human reads."""
    n = label_node(text="I consent to the processing of my personal data")
    assert label_matches(n, re.compile(r"(?i)i consent to .*"))
    assert not label_matches(n, re.compile(r"(?i)submit"))


def test_label_is_not_focusable_so_press_key_still_refuses_it():
    """This feature is click-shaped; it must not widen press-key."""
    assert not is_focusable_control(label_node())
