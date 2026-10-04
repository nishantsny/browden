"""Tests for the composed write-action gate sequences.

These exercise the gate functions moved out of ``server.py`` directly — no
session or FastMCP in the picture, just the allowlist + a serialized element
node. The tool-level tests (``test_server_click`` / ``test_server_insert_text``)
still cover the same gates end-to-end; this file pins the composition unit.
"""
import pytest

from safe_agent_browser.mcp.validator import (
    MAX_UPLOAD_BYTES,
    upload_file_gate,
    SafeAgentBrowserAccessRuleSet,
    SafeAgentBrowserRuntimeConfiguration,
    ValidationError,
    check_action_host,
    validate_click_target,
    validate_press_key_target,
    validate_upload_path,
    validate_upload_target,
    validate_write_text_target,
)

# amazon.com readable + click/write-text enabled; nothing else is.
_RULES = SafeAgentBrowserAccessRuleSet({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"amazon.com": [".*"], "wholefoodsmarket.com": [".*"]}},
    "click": {"amazon.com": {"paths": [".*"], "label": r"(?i)\badd to cart\b"}},
    "write-text": {"amazon.com": {"paths": [".*"], "label": r"(?i)grocery tip.*",
                                  "field_ids": ["tip-amount"]}},
})

_DENIED = SafeAgentBrowserAccessRuleSet({
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
    check_action_host(_RULES, "click", AMAZON)  # no raise


def test_host_not_on_section_rejected():
    with pytest.raises(ValidationError, match="no click rule authorizes"):
        check_action_host(_RULES, "click", "https://evil.example.com/p")


def test_denylist_vetoes_even_allowed_host():
    with pytest.raises(ValidationError, match="denylist"):
        check_action_host(_DENIED, "click", AMAZON)


# -- validate_click_target (Gates 2, 2b, 3) ----------------------------------

def test_click_happy_path():
    validate_click_target(_RULES, AMAZON, "#atc", _found(_atc()))  # no raise


def test_click_no_match_rejected():
    with pytest.raises(ValidationError, match="no element matches"):
        validate_click_target(_RULES, AMAZON, "#x", _found())


def test_click_ambiguous_rejected():
    with pytest.raises(ValidationError, match="ambiguous"):
        validate_click_target(_RULES, AMAZON, ".a", _found(_atc(), _atc()))


def test_click_decoy_rejected():
    node = _atc(**{"data-target-audience": "ai-agent"})
    with pytest.raises(ValidationError, match="decoy"):
        validate_click_target(_RULES, AMAZON, "#d", _found(node))


def test_click_label_mismatch_rejected():
    with pytest.raises(ValidationError, match="does not match any click label"):
        validate_click_target(_RULES, AMAZON, "#x", _found(_atc(value="Add to bag")))


def test_click_anchor_off_read_allowlist_rejected():
    node = _anchor("https://evil.example/x")
    with pytest.raises(ValidationError, match="not on the read allowlist"):
        validate_click_target(_RULES, AMAZON, "a.evil", _found(node))


def test_click_anchor_cross_domain_allowlisted_passes():
    node = _anchor("https://www.wholefoodsmarket.com/cart")
    # cross-domain but the target is read-allowed, and click label is '(?i)add to
    # cart' — anchor text won't match, so use the allow-any host for this one.
    allow_any = SafeAgentBrowserAccessRuleSet({
        "read": {"enabled": True, "tranco": {"enabled": False},
                 "website_overrides": {"amazon.com": [".*"], "wholefoodsmarket.com": [".*"]}},
        "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},
    })
    validate_click_target(allow_any, AMAZON, "a.wf", _found(node))  # no raise


def test_click_anchor_mailto_rejected():
    node = _anchor("mailto:help@amazon.com", text="Contact")
    with pytest.raises(ValidationError, match="non-navigational scheme"):
        validate_click_target(_RULES, AMAZON, "a.mail", _found(node))


# -- validate_write_text_target (Gates 2, 3) ---------------------------------

def test_write_text_happy_path_by_label():
    validate_write_text_target(_RULES, AMAZON, "#tip", _found(_field()))  # no raise


def test_write_text_by_field_id_escape_hatch():
    # A label-less box authorized by its exact id/name in field_ids.
    node = _field(label="", node_id="tip-amount")
    validate_write_text_target(_RULES, AMAZON, "#tip", _found(node))  # no raise


def test_write_text_non_text_control_rejected():
    button = {"tag": "button", "id": None, "classes": [], "attributes": {}, "text": "Go"}
    with pytest.raises(ValidationError, match="fillable text control"):
        validate_write_text_target(_RULES, AMAZON, "#b", _found(button))


def test_write_text_label_mismatch_rejected():
    with pytest.raises(ValidationError, match="does not match any write-text rule"):
        validate_write_text_target(_RULES, AMAZON, "#x", _found(_field(label="Coupon code")))

# -- validate_press_key_target (Gates 2, 2b, 3) ------------------------------

# cronometer.com press-key enabled: Enter + up/down arrows on the app path '/'.
_PK = SafeAgentBrowserAccessRuleSet({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"cronometer.com": [".*"]}},
    "press-key": {"cronometer.com": [
        {"path": ["^/$"], "label": ".*", "keys": ["Enter", "ArrowDown", "ArrowUp"]},
    ]},
})
# same, but the rule only admits rows whose text starts with "Fried".
_PK_LABELLED = SafeAgentBrowserAccessRuleSet({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"cronometer.com": [".*"]}},
    "press-key": {"cronometer.com": [
        {"path": ["^/$"], "label": r"(?i)fried.*", "keys": ["Enter"]},
    ]},
})
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

_SCRATCH = SafeAgentBrowserRuntimeConfiguration({"profiles": {"/profiles/scratch": {
    "allow_all": True, "read": {"tranco": {"enabled": False}}}}}).access_rules_for("/profiles/scratch")


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
    denied = SafeAgentBrowserRuntimeConfiguration({
        "denylist": {"blocked.test": [".*"]},
        "profiles": {"/profiles/scratch": {"allow_all": True}},
    }).access_rules_for("/profiles/scratch")
    with pytest.raises(ValidationError, match="denylist"):
        check_action_host(denied, "click", "https://blocked.test/x")


def test_allow_all_still_gates_where_an_anchor_would_navigate():
    # The anchor check runs against the same read policy, so a link off to an
    # unranked host is refused even in an allow_all profile with Tranco on.
    scratch = SafeAgentBrowserRuntimeConfiguration({"profiles": {"/profiles/s": {"allow_all": True}}}).access_rules_for("/profiles/s")
    anchor = {"tag": "a", "id": None, "classes": [],
              "attributes": {"href": "https://unranked.test/x"}, "text": "Go"}
    with pytest.raises(ValidationError, match="not on the read allowlist"):
        validate_click_target(scratch, "https://google.com/", "a", _found(anchor))


# -- upload-file: the control gate (gates 2 and 3) ----------------------------

SPLITWISE = "https://secure.splitwise.com/"

_UPLOAD_RULES = SafeAgentBrowserAccessRuleSet({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"secure.splitwise.com": [".*"]}},
    "upload-file": {"secure.splitwise.com": [
        {"path": [".*"], "label": r"(?i)receipt", "field_ids": ["bill_file_expense"]}]},
    # The same host is trusted to type, with an "any label" rule — the privilege
    # that widening _TEXT_INPUT_TYPES would have silently turned into an upload.
    "write-text": {"secure.splitwise.com": {"paths": [".*"], "label": ".*"}},
})


def _file_input(node_id=None, **attrs):
    return {"tag": "input", "id": node_id, "classes": [],
            "attributes": {"type": "file", **attrs}, "text": ""}


def test_upload_authorized_by_field_id():
    """The Splitwise case: no visible label of any kind, so the id is the handle."""
    validate_upload_target(_UPLOAD_RULES, SPLITWISE, "#bill_file_expense",
                           _found(_file_input(node_id="bill_file_expense")))


def test_upload_authorized_by_visible_label():
    validate_upload_target(_UPLOAD_RULES, SPLITWISE, "input[type=file]",
                           _found(_file_input(**{"aria-label": "Receipt"})))


def test_upload_unmatched_label_and_unlisted_id_rejected():
    with pytest.raises(ValidationError, match="does not match any upload-file rule"):
        validate_upload_target(_UPLOAD_RULES, SPLITWISE, "#other",
                               _found(_file_input(node_id="avatar_file")))


def test_upload_non_file_input_rejected():
    """A write-text rule admitting any label must not become a file channel."""
    text_box = {"tag": "input", "id": "bill_file_expense", "classes": [],
                "attributes": {"type": "text"}, "text": ""}
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_UPLOAD_RULES, SPLITWISE, "#bill_file_expense", _found(text_box))


def test_upload_decoy_rejected():
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_UPLOAD_RULES, SPLITWISE, "#x",
                               _found(_file_input(node_id="bill_file_expense",
                                                  **{"data-agent-action": "upload"})))


def test_upload_disabled_rejected():
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_UPLOAD_RULES, SPLITWISE, "#x",
                               _found(_file_input(node_id="bill_file_expense", disabled="")))


def test_upload_ambiguous_selector_rejected():
    with pytest.raises(ValidationError, match="ambiguous"):
        validate_upload_target(_UPLOAD_RULES, SPLITWISE, "input",
                               _found(_file_input(node_id="bill_file_expense"),
                                      _file_input(node_id="other")))


def test_upload_host_not_listed_for_upload_rejected():
    """Trusted to type is not trusted to upload: the sections are independent."""
    typing_only = SafeAgentBrowserAccessRuleSet({
        "write-text": {"secure.splitwise.com": {"paths": [".*"], "label": ".*"}}})
    with pytest.raises(ValidationError, match="no upload-file rule authorizes"):
        check_action_host(typing_only, "upload-file", SPLITWISE)


# -- upload-file: the filesystem gate (gate 4) --------------------------------

@pytest.fixture
def allowed(tmp_path):
    """The operator's allowed upload locations, with one receipt in one of them."""
    location = tmp_path / "receipts"
    location.mkdir()
    (location / "lunch.png").write_bytes(b"png")
    return (location,)


def test_a_file_under_an_allowed_location_passes(allowed, tmp_path):
    resolved = validate_upload_path(allowed, str(tmp_path / "receipts" / "lunch.png"))
    assert resolved == tmp_path / "receipts" / "lunch.png"


def test_no_roots_configured_denies_everything(tmp_path):
    (tmp_path / "f.png").write_bytes(b"x")
    with pytest.raises(ValidationError, match="no allowed_upload_locations are configured"):
        validate_upload_path((), str(tmp_path / "f.png"))


def test_a_file_outside_every_allowed_location_is_refused(allowed, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        validate_upload_path(allowed, str(secret))


def test_traversal_out_of_an_allowed_location_is_refused(allowed, tmp_path):
    """The path is resolved before it is compared, so `../` buys nothing."""
    (tmp_path / "id_rsa").write_bytes(b"PRIVATE KEY")
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        validate_upload_path(allowed, str(tmp_path / "receipts" / ".." / "id_rsa"))


def test_a_symlink_inside_an_allowed_location_pointing_out_is_refused(allowed, tmp_path):
    """The dangerous case: the path *is* under an allowed location; its target is not."""
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    link = allowed[0] / "innocent.png"
    link.symlink_to(secret)
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        validate_upload_path(allowed, str(link))


def test_a_symlinked_location_still_admits_its_own_files(tmp_path):
    """The mirror image: a location reached through a symlink must still work."""
    real = tmp_path / "real-receipts"
    real.mkdir()
    (real / "lunch.png").write_bytes(b"png")
    link = tmp_path / "receipts"
    link.symlink_to(real)
    # The rule set resolves each location on load; this is that resolved form.
    validate_upload_path((real.resolve(),), str(link / "lunch.png"))


def test_a_missing_file_is_refused(allowed, tmp_path):
    with pytest.raises(ValidationError, match="not an existing regular file"):
        validate_upload_path(allowed, str(tmp_path / "receipts" / "nope.png"))


def test_a_directory_is_refused(allowed, tmp_path):
    (tmp_path / "receipts" / "sub").mkdir()
    with pytest.raises(ValidationError, match="not an existing regular file"):
        validate_upload_path(allowed, str(tmp_path / "receipts" / "sub"))


def test_an_oversize_file_is_refused(allowed, tmp_path):
    big = tmp_path / "receipts" / "big.bin"
    with big.open("wb") as fh:
        fh.truncate(MAX_UPLOAD_BYTES + 1)
    with pytest.raises(ValidationError, match="over the .* upload cap"):
        validate_upload_path(allowed, str(big))


def test_allow_all_does_not_grant_the_filesystem():
    """A scratch profile may act on any page, and still upload nothing."""
    scratch = SafeAgentBrowserRuntimeConfiguration({
        "profiles": {"/profiles/scratch": {"allow_all": True}}}).access_rules_for("/profiles/scratch")
    check_action_host(scratch, "upload-file", "https://unlisted.test/form")  # page authority: yes
    assert scratch.allowed_upload_locations == ()
    with pytest.raises(ValidationError, match="no allowed_upload_locations are configured"):
        validate_upload_path(scratch.allowed_upload_locations, "/etc/passwd")


def test_a_path_with_a_newline_is_refused(allowed, tmp_path):
    """Selenium splits a path on newlines, so one path could name two files.

    The driver hands the path to the browser as keystrokes; a `multiple` input
    given "a\nb" ends up holding BOTH, and only the first was ever judged. A
    control character has no place in a path safe-agent-browser was asked to upload.
    """
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    smuggled = allowed[0] / "lunch.png"
    with pytest.raises(ValidationError, match="control character"):
        validate_upload_path(allowed, f"{smuggled}\n{secret}")


def test_other_control_characters_are_refused_too(allowed):
    for ch in ("\r", "\t", "\x00", "\x7f"):
        with pytest.raises(ValidationError, match="control character"):
            validate_upload_path(allowed, f"{allowed[0] / 'lunch.png'}{ch}")


def test_the_gate_carries_the_path_it_admitted(allowed):
    """The action reads the file off the gate; nothing downstream re-resolves it."""
    rules = SafeAgentBrowserAccessRuleSet({
        "upload-file": {"*": {"paths": [".*"], "label": ".*"}},
        "allowed_upload_locations": [str(allowed[0])],
    })
    gate = upload_file_gate(rules, str(allowed[0] / "lunch.png"))
    gate.check_page("https://anything.test/form")
    assert gate.admitted.path == allowed[0] / "lunch.png"


def test_a_gate_that_refused_carries_no_path(allowed, tmp_path):
    rules = SafeAgentBrowserAccessRuleSet({
        "upload-file": {"*": {"paths": [".*"], "label": ".*"}},
        "allowed_upload_locations": [str(allowed[0])],
    })
    gate = upload_file_gate(rules, str(tmp_path / "id_rsa"))
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        gate.check_page("https://anything.test/form")
    with pytest.raises(ValidationError, match="no file was admitted"):
        gate.admitted.path
