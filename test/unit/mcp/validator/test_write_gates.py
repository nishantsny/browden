"""Tests for the composed write-action gate sequences.

These exercise the gate functions moved out of ``server.py`` directly — no
session or MCPServer in the picture, just the allowlist + a serialized element
node. The tool-level tests (``test_server_click`` / ``test_server_insert_text``)
still cover the same gates end-to-end; this file pins the composition unit.
"""
import pytest

from browden.mcp.validator import (
    DEFAULT_MAX_UPLOAD_BYTES,
    upload_file_gate,
    BrowdenAccessRuleSet,
    BrowdenRuntimeConfiguration,
    ValidationError,
    check_action_host,
    validate_click_target,
    validate_press_key_target,
    validate_upload_path,
    validate_upload_target,
    validate_write_text_target,
)

# amazon.com readable + click/write-text enabled; nothing else is.
_RULES = BrowdenAccessRuleSet({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"amazon.com": [".*"], "wholefoodsmarket.com": [".*"]}},
    "click": {"amazon.com": {"paths": [".*"], "label": r"(?i)\badd to cart\b"}},
    "write-text": {"amazon.com": {"paths": [".*"], "label": r"(?i)grocery tip.*",
                                  "field_ids": ["tip-amount"]}},
})

_DENIED = BrowdenAccessRuleSet({
    "denylist": {"amazon.com": [".*"]},
    "click": {"amazon.com": {"paths": [".*"], "label": ".*"}},
})

AMAZON = "https://www.amazon.com/dp/B0FBRRM2VQ"

# The size cap handed to validate_upload_path directly (the gate passes the rule
# set's effective one); the built-in default, so nothing here depends on config.
_CAP = DEFAULT_MAX_UPLOAD_BYTES


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
    allow_any = BrowdenAccessRuleSet({
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
_PK = BrowdenAccessRuleSet({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"cronometer.com": [".*"]}},
    "press-key": {"cronometer.com": [
        {"path": ["^/$"], "label": ".*", "keys": ["Enter", "ArrowDown", "ArrowUp"]},
    ]},
})
# same, but the rule only admits rows whose text starts with "Fried".
_PK_LABELLED = BrowdenAccessRuleSet({
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

_SCRATCH = BrowdenRuntimeConfiguration({"profiles": {"/profiles/scratch": {
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
    denied = BrowdenRuntimeConfiguration({
        "denylist": {"blocked.test": [".*"]},
        "profiles": {"/profiles/scratch": {"allow_all": True}},
    }).access_rules_for("/profiles/scratch")
    with pytest.raises(ValidationError, match="denylist"):
        check_action_host(denied, "click", "https://blocked.test/x")


def test_allow_all_still_gates_where_an_anchor_would_navigate():
    # The anchor check runs against the same read policy, so a link off to an
    # unranked host is refused even in an allow_all profile with Tranco on.
    scratch = BrowdenRuntimeConfiguration({"profiles": {"/profiles/s": {"allow_all": True}}}).access_rules_for("/profiles/s")
    anchor = {"tag": "a", "id": None, "classes": [],
              "attributes": {"href": "https://unranked.test/x"}, "text": "Go"}
    with pytest.raises(ValidationError, match="not on the read allowlist"):
        validate_click_target(scratch, "https://google.com/", "a", _found(anchor))


# -- upload-file: the control gate (gates 2 and 3) ----------------------------

SPLITWISE = "https://secure.splitwise.com/"

_UPLOAD_RULES = BrowdenAccessRuleSet({
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
    typing_only = BrowdenAccessRuleSet({
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
    resolved = validate_upload_path(allowed, str(tmp_path / "receipts" / "lunch.png"), max_upload_bytes=_CAP)
    assert resolved == tmp_path / "receipts" / "lunch.png"


def test_no_roots_configured_denies_everything(tmp_path):
    (tmp_path / "f.png").write_bytes(b"x")
    with pytest.raises(ValidationError, match="no allowed_upload_locations are configured"):
        validate_upload_path((), str(tmp_path / "f.png"), max_upload_bytes=_CAP)


def test_a_file_outside_every_allowed_location_is_refused(allowed, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        validate_upload_path(allowed, str(secret), max_upload_bytes=_CAP)


def test_traversal_out_of_an_allowed_location_is_refused(allowed, tmp_path):
    """The path is resolved before it is compared, so `../` buys nothing."""
    (tmp_path / "id_rsa").write_bytes(b"PRIVATE KEY")
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        validate_upload_path(allowed, str(tmp_path / "receipts" / ".." / "id_rsa"), max_upload_bytes=_CAP)


def test_a_symlink_inside_an_allowed_location_pointing_out_is_refused(allowed, tmp_path):
    """The dangerous case: the path *is* under an allowed location; its target is not."""
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    link = allowed[0] / "innocent.png"
    link.symlink_to(secret)
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        validate_upload_path(allowed, str(link), max_upload_bytes=_CAP)


def test_a_symlinked_location_still_admits_its_own_files(tmp_path):
    """The mirror image: a location reached through a symlink must still work."""
    real = tmp_path / "real-receipts"
    real.mkdir()
    (real / "lunch.png").write_bytes(b"png")
    link = tmp_path / "receipts"
    link.symlink_to(real)
    # The rule set resolves each location on load; this is that resolved form.
    validate_upload_path((real.resolve(),), str(link / "lunch.png"), max_upload_bytes=_CAP)


def test_a_missing_file_is_refused(allowed, tmp_path):
    with pytest.raises(ValidationError, match="not an existing regular file"):
        validate_upload_path(allowed, str(tmp_path / "receipts" / "nope.png"), max_upload_bytes=_CAP)


def test_a_directory_is_refused(allowed, tmp_path):
    (tmp_path / "receipts" / "sub").mkdir()
    with pytest.raises(ValidationError, match="not an existing regular file"):
        validate_upload_path(allowed, str(tmp_path / "receipts" / "sub"), max_upload_bytes=_CAP)


def test_an_oversize_file_is_refused(allowed, tmp_path):
    big = tmp_path / "receipts" / "big.bin"
    with big.open("wb") as fh:
        fh.truncate(_CAP + 1)
    with pytest.raises(ValidationError, match="over the .* upload cap"):
        validate_upload_path(allowed, str(big), max_upload_bytes=_CAP)


def test_allow_all_does_not_grant_the_filesystem():
    """A scratch profile may act on any page, and still upload nothing."""
    scratch = BrowdenRuntimeConfiguration({
        "profiles": {"/profiles/scratch": {"allow_all": True}}}).access_rules_for("/profiles/scratch")
    check_action_host(scratch, "upload-file", "https://unlisted.test/form")  # page authority: yes
    assert scratch.allowed_upload_locations == ()
    with pytest.raises(ValidationError, match="no allowed_upload_locations are configured"):
        validate_upload_path(scratch.allowed_upload_locations, "/etc/passwd", max_upload_bytes=_CAP)


def test_a_path_with_a_newline_is_refused(allowed, tmp_path):
    """Selenium splits a path on newlines, so one path could name two files.

    The driver hands the path to the browser as keystrokes; a `multiple` input
    given "a\nb" ends up holding BOTH, and only the first was ever judged. A
    control character has no place in a path browden was asked to upload.
    """
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    smuggled = allowed[0] / "lunch.png"
    with pytest.raises(ValidationError, match="control character"):
        validate_upload_path(allowed, f"{smuggled}\n{secret}", max_upload_bytes=_CAP)


def test_other_control_characters_are_refused_too(allowed):
    for ch in ("\r", "\t", "\x00", "\x7f"):
        with pytest.raises(ValidationError, match="control character"):
            validate_upload_path(allowed, f"{allowed[0] / 'lunch.png'}{ch}", max_upload_bytes=_CAP)


def test_the_gate_carries_the_path_it_admitted(allowed):
    """The action reads the file off the gate; nothing downstream re-resolves it."""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"*": {"paths": [".*"], "label": ".*"}},
        "allowed_upload_locations": [str(allowed[0])],
    })
    gate = upload_file_gate(rules, str(allowed[0] / "lunch.png"))
    gate.check_page("https://anything.test/form")
    assert gate.admitted.path == allowed[0] / "lunch.png"


def test_a_gate_that_refused_carries_no_path(allowed, tmp_path):
    rules = BrowdenAccessRuleSet({
        "upload-file": {"*": {"paths": [".*"], "label": ".*"}},
        "allowed_upload_locations": [str(allowed[0])],
    })
    gate = upload_file_gate(rules, str(tmp_path / "id_rsa"))
    with pytest.raises(ValidationError, match="outside every allowed upload location"):
        gate.check_page("https://anything.test/form")
    with pytest.raises(ValidationError, match="no file was admitted"):
        gate.admitted.path


# -- upload-file: a hidden file input, admitted only by an opted-in field_id ---

STUDIO = "https://studio.youtube.com/channel/UC123"

_HIDDEN_UPLOAD_RULES = BrowdenAccessRuleSet({
    "upload-file": {"studio.youtube.com": [
        {"path": ["^/channel/.*"], "label": r"(?i)select files", "field_ids": ["Filedata"],
         "hidden_input": True}]},
})


def _studio_input(**extra):
    """YouTube Studio's real upload control, as observed 2026-11-22."""
    return _file_input(name="Filedata", multiple="", tabindex="-1",
                       style="position: absolute; opacity: 0; display: none;",
                       **{"aria-hidden": "true", **extra})


def test_a_hidden_input_is_admitted_by_an_opted_in_field_id():
    assert validate_upload_target(_HIDDEN_UPLOAD_RULES, STUDIO, "input[name=Filedata]",
                                  _found(_studio_input())) is True


def test_an_opted_in_input_is_reported_hidden_even_when_its_attributes_look_visible():
    """A stylesheet class or a hidden ancestor hides it where the snapshot can't see.

    The backend's live visibility check would refuse it, so an opted-in match
    must waive that check whatever the input's own attributes say.
    """
    assert validate_upload_target(_HIDDEN_UPLOAD_RULES, STUDIO, "input[name=Filedata]",
                                  _found(_file_input(name="Filedata", **{"class": "sr-only"}))) is True


def test_an_input_admitted_without_the_opt_in_is_not_reported_hidden():
    assert validate_upload_target(_UPLOAD_RULES, SPLITWISE, "#bill_file_expense",
                                  _found(_file_input(node_id="bill_file_expense"))) is False


def test_the_opt_in_covers_only_its_own_rules_field_ids():
    """A field named by a rule WITHOUT hidden_input stays visible-only, even on a
    page where another rule opts a different field in."""
    rules = BrowdenAccessRuleSet({"upload-file": {"studio.youtube.com": [
        {"path": [".*"], "label": "(?!)", "field_ids": ["Filedata"], "hidden_input": True},
        {"path": [".*"], "label": "(?!)", "field_ids": ["thumbnail"]}]}})
    assert validate_upload_target(rules, STUDIO, "input",
                                  _found(_file_input(name="thumbnail"))) is False
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(rules, STUDIO, "input",
                               _found(_file_input(name="thumbnail", style="display:none")))


def test_a_hidden_input_is_refused_without_the_opt_in():
    rules = BrowdenAccessRuleSet({"upload-file": {"studio.youtube.com": [
        {"path": [".*"], "label": ".*", "field_ids": ["Filedata"]}]}})
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(rules, STUDIO, "input", _found(_studio_input()))


def test_a_hidden_input_is_never_admitted_by_its_label():
    """The opt-in widens field_ids only: a page-supplied label never vouches for a hidden control."""
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_HIDDEN_UPLOAD_RULES, STUDIO, "input",
                               _found(_file_input(name="other", style="display:none",
                                                  **{"aria-label": "Select files"})))


def test_a_hidden_input_with_an_unlisted_name_is_refused():
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_HIDDEN_UPLOAD_RULES, STUDIO, "input",
                               _found(_file_input(name="avatar", style="display:none")))


def test_the_opt_in_only_covers_the_pages_its_rule_matches():
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_HIDDEN_UPLOAD_RULES, "https://studio.youtube.com/settings",
                               "input", _found(_studio_input()))


def test_the_opt_in_does_not_admit_a_disabled_or_decoy_hidden_input():
    for extra in ({"disabled": ""}, {"data-agent-action": "upload"}):
        with pytest.raises(ValidationError, match="not a file input"):
            validate_upload_target(_HIDDEN_UPLOAD_RULES, STUDIO, "input",
                                   _found(_studio_input(**extra)))


def test_the_gate_records_a_hidden_admission_for_the_action(tmp_path):
    location = tmp_path / "uploads"
    location.mkdir()
    (location / "short.mp4").write_bytes(b"mp4")
    rules = BrowdenAccessRuleSet({
        "upload-file": {"studio.youtube.com": [
            {"path": [".*"], "label": "(?!)", "field_ids": ["Filedata"], "hidden_input": True}]},
        "allowed_upload_locations": [str(location)],
    })
    gate = upload_file_gate(rules, str(location / "short.mp4"))
    assert gate.control.hidden is False
    gate.check_page(STUDIO)
    gate.check_element(STUDIO, "input", _found(_studio_input()))
    assert gate.control.hidden is True


# -- upload-file: the size cap (global max_upload_bytes + per-rule max_bytes) ---

UPLOADS = "https://up.test/upload"
MIB = 1024 * 1024


def _sparse(path, size):
    """A file of ``size`` bytes that takes no disk space (only its length matters)."""
    with path.open("wb") as fh:
        fh.truncate(size)
    return path


def _cap_rules(location, *, rule_max_bytes=None, **top):
    rule = {"path": ["^/upload$"], "label": ".*", "field_ids": ["file"]}
    if rule_max_bytes is not None:
        rule["max_bytes"] = rule_max_bytes
    return BrowdenAccessRuleSet({
        "upload-file": {"up.test": [rule]},
        "allowed_upload_locations": [str(location)],
        **top,
    })


def _run_upload_gate(rules, path, node=None):
    gate = upload_file_gate(rules, str(path))
    gate.check_page(UPLOADS)
    gate.check_element(UPLOADS, "input[name=file]", _found(node or _file_input(name="file")))
    return gate


def test_the_builtin_default_cap_is_one_mib(allowed):
    rules = _cap_rules(allowed[0])
    assert rules.max_upload_bytes == MIB == DEFAULT_MAX_UPLOAD_BYTES
    _run_upload_gate(rules, _sparse(allowed[0] / "ok.bin", MIB))  # exactly at the cap
    with pytest.raises(ValidationError, match=rf"is {MIB + 1} bytes, over the {MIB}-byte upload cap"):
        _run_upload_gate(rules, _sparse(allowed[0] / "big.bin", MIB + 1))


def test_a_file_between_the_old_25_mib_cap_and_a_configured_default_is_accepted(allowed):
    """The live case: a 72 MB video under a raised cap; the old hard-coded 25 MiB refused it."""
    rules = _cap_rules(allowed[0], max_upload_bytes=100 * MIB)
    video = _sparse(allowed[0] / "video.mp4", 72_000_000)
    assert _run_upload_gate(rules, video).admitted.path == video


def test_a_file_over_the_configured_default_is_refused_with_that_number(allowed):
    rules = _cap_rules(allowed[0], max_upload_bytes=4096)
    with pytest.raises(ValidationError, match="over the 4096-byte upload cap"):
        _run_upload_gate(rules, _sparse(allowed[0] / "big.bin", 4097))


def test_a_rule_max_bytes_admits_a_file_between_the_default_and_its_cap(allowed):
    rules = _cap_rules(allowed[0], rule_max_bytes=100 * MIB)
    video = _sparse(allowed[0] / "video.mp4", 72_000_000)
    assert _run_upload_gate(rules, video).admitted.path == video


def test_a_file_over_the_rule_max_bytes_is_refused_naming_it(allowed):
    rules = _cap_rules(allowed[0], rule_max_bytes=100 * MIB)
    with pytest.raises(ValidationError, match=f"over the {100 * MIB}-byte upload cap"):
        _run_upload_gate(rules, _sparse(allowed[0] / "huge.mp4", 100 * MIB + 1))


def test_a_rule_max_bytes_can_be_below_the_default(allowed):
    rules = _cap_rules(allowed[0], rule_max_bytes=10)
    with pytest.raises(ValidationError, match="over the 10-byte upload cap"):
        _run_upload_gate(rules, _sparse(allowed[0] / "small.bin", 11))


def test_the_cap_is_that_of_the_rule_that_admits_the_control(allowed):
    """Another rule on the same page with a bigger cap does not lend it to this control.

    The early check (before the DOM is read) passes on the page's most generous
    rule; the element check then holds the file to the rule that admitted it.
    """
    # A label no control can match, so each rule admits only the field it names.
    big = _sparse(allowed[0] / "video.mp4", 2 * MIB)
    strict = BrowdenAccessRuleSet({
        "upload-file": {"up.test": [
            {"path": ["^/upload$"], "label": "(?!)", "field_ids": ["video"], "max_bytes": 100 * MIB},
            {"path": ["^/upload$"], "label": "(?!)", "field_ids": ["receipt"]}]},
        "allowed_upload_locations": [str(allowed[0])],
    })
    _run_upload_gate(strict, big, _file_input(name="video"))
    with pytest.raises(ValidationError, match=f"over the {MIB}-byte upload cap"):
        _run_upload_gate(strict, big, _file_input(name="receipt"))


def test_a_file_over_every_rule_on_the_page_is_refused_before_the_dom_is_read(allowed):
    rules = _cap_rules(allowed[0], rule_max_bytes=2 * MIB)
    gate = upload_file_gate(rules, str(_sparse(allowed[0] / "huge.bin", 2 * MIB + 1)))
    with pytest.raises(ValidationError, match=f"over the {2 * MIB}-byte upload cap"):
        gate.check_page(UPLOADS)


def test_a_profile_rule_max_bytes_applies_in_that_profile_only(allowed):
    configuration = BrowdenRuntimeConfiguration({
        "allowed_upload_locations": [str(allowed[0])],
        "upload-file": {"up.test": [{"path": ["^/upload$"], "label": ".*"}]},
        "profiles": {"/profiles/video": {"upload-file": {"up.test": [
            {"path": ["^/upload$"], "label": ".*", "max_bytes": 100 * MIB}]}}},
    })
    video = _sparse(allowed[0] / "video.mp4", 72_000_000)
    _run_upload_gate(configuration.access_rules_for("/profiles/video"), video)
    with pytest.raises(ValidationError, match=f"over the {MIB}-byte upload cap"):
        _run_upload_gate(configuration.access_rules_for("/profiles/other"), video)


def test_two_rules_admitting_the_same_control_give_it_the_larger_cap(allowed):
    """Rules are additive: a small max_bytes cannot tighten what a broader rule admits."""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"up.test": [
            {"path": ["^/upload$"], "label": ".*"},                                   # default 1 MiB
            {"path": ["^/upload$"], "label": "(?!)", "field_ids": ["file"], "max_bytes": 10}]},
        "allowed_upload_locations": [str(allowed[0])],
    })
    _run_upload_gate(rules, _sparse(allowed[0] / "half.bin", MIB // 2))
    with pytest.raises(ValidationError, match=f"over the {MIB}-byte upload cap"):
        _run_upload_gate(rules, _sparse(allowed[0] / "big.bin", MIB + 1))


def test_the_cap_applies_on_the_hidden_input_branch(allowed):
    rules = BrowdenAccessRuleSet({
        "upload-file": {"up.test": [
            {"path": ["^/upload$"], "label": "(?!)", "field_ids": ["file"],
             "hidden_input": True, "max_bytes": 2 * MIB}]},
        "allowed_upload_locations": [str(allowed[0])],
    })
    hidden = _file_input(name="file", style="display: none;")
    gate = _run_upload_gate(rules, _sparse(allowed[0] / "ok.mp4", 2 * MIB), hidden)
    assert gate.control.hidden is True
    with pytest.raises(ValidationError, match=f"over the {2 * MIB}-byte upload cap"):
        _run_upload_gate(rules, _sparse(allowed[0] / "big.mp4", 2 * MIB + 1), hidden)


def test_a_hidden_input_rule_on_a_hidden_control_does_not_borrow_a_label_rules_cap(allowed):
    """A label never admits a hidden control, so a label rule's cap doesn't apply to one."""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"up.test": [
            {"path": ["^/upload$"], "label": "(?!)", "field_ids": ["file"], "hidden_input": True},
            {"path": ["^/upload$"], "label": ".*", "max_bytes": 100 * MIB}]},
        "allowed_upload_locations": [str(allowed[0])],
    })
    with pytest.raises(ValidationError, match=f"over the {MIB}-byte upload cap"):
        _run_upload_gate(rules, _sparse(allowed[0] / "big.mp4", 2 * MIB),
                         _file_input(name="file", style="display: none;"))


def test_a_visible_control_named_by_a_hidden_input_rule_keeps_a_bigger_label_rule_cap(allowed):
    """The hidden branch must not drop the other rules that admit a visible control."""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"up.test": [
            {"path": ["^/upload$"], "label": "(?!)", "field_ids": ["file"], "hidden_input": True},
            {"path": ["^/upload$"], "label": ".*", "max_bytes": 100 * MIB}]},
        "allowed_upload_locations": [str(allowed[0])],
    })
    _run_upload_gate(rules, _sparse(allowed[0] / "video.mp4", 72_000_000), _file_input(name="file"))


def test_an_allow_all_profile_uploads_under_the_default_cap(allowed):
    configuration = BrowdenRuntimeConfiguration({
        "allowed_upload_locations": [str(allowed[0])],
        "max_upload_bytes": 4096,
        "profiles": {"/profiles/scratch": {"allow_all": True}},
    })
    scratch = configuration.access_rules_for("/profiles/scratch")
    _run_upload_gate(scratch, _sparse(allowed[0] / "ok.bin", 4096))
    with pytest.raises(ValidationError, match="over the 4096-byte upload cap"):
        _run_upload_gate(scratch, _sparse(allowed[0] / "big.bin", 4097))


def test_an_allow_all_profile_keeps_a_listed_rules_bigger_cap(allowed):
    configuration = BrowdenRuntimeConfiguration({
        "allowed_upload_locations": [str(allowed[0])],
        "profiles": {"/profiles/scratch": {
            "allow_all": True,
            "upload-file": {"up.test": [{"path": ["^/upload$"], "label": ".*",
                                         "max_bytes": 100 * MIB}]}}},
    })
    _run_upload_gate(configuration.access_rules_for("/profiles/scratch"),
                     _sparse(allowed[0] / "video.mp4", 72_000_000))


@pytest.mark.parametrize("bad", [0, -1, True, "10", 1.5])
def test_a_rule_set_built_directly_refuses_an_invalid_max_bytes(bad):
    with pytest.raises(ValueError, match="max_bytes must be a positive integer"):
        BrowdenAccessRuleSet({"upload-file": {"up.test": [
            {"path": [".*"], "label": ".*", "max_bytes": bad}]}})


@pytest.mark.parametrize("bad", [None, 0, -1, False, "10", 1.5])
def test_a_rule_set_built_directly_refuses_an_invalid_max_upload_bytes(bad):
    with pytest.raises(ValueError, match="max_upload_bytes must be a positive integer"):
        BrowdenAccessRuleSet({"max_upload_bytes": bad})


# -- upload-file: a hidden input with no id/name, named by its accept value ----

TIKTOK = "https://www.tiktok.com/tiktokstudio/upload"

_ACCEPT_UPLOAD_RULES = BrowdenAccessRuleSet({
    "upload-file": {"www.tiktok.com": [{
        "path": ["^/tiktokstudio/upload$"], "label": ".*", "hidden_input": True,
        "accept": ["video/*"]}]},
})


def _tiktok_input(**extra):
    """TikTok Studio's only upload control, as observed 2026-11-24: no id, name or label."""
    return _file_input(accept="video/*", style="display: none;", **extra)


def _on_page(target, *others):
    """``found`` for a selector that matched ``target``, on a page whose file
    inputs are ``target`` plus ``others`` (the survey the session takes)."""
    return {**_found(target), "survey": [target, *others]}


def test_a_hidden_id_less_input_is_admitted_by_its_accept_value():
    assert validate_upload_target(_ACCEPT_UPLOAD_RULES, TIKTOK, "input[type=file]",
                                  _on_page(_tiktok_input())) is True


def test_a_hidden_id_less_input_is_refused_without_accept():
    """The live blocker: `label: '.*'` alone never admits a hidden input."""
    rules = BrowdenAccessRuleSet({"upload-file": {"www.tiktok.com": [{
        "path": ["^/tiktokstudio/upload$"], "label": ".*"}]}})
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(rules, TIKTOK, "input[type=file]", _on_page(_tiktok_input()))


def test_a_hidden_input_with_a_different_accept_value_is_refused():
    rules = BrowdenAccessRuleSet({"upload-file": {"www.tiktok.com": [{
        "path": [".*"], "label": ".*", "hidden_input": True, "accept": ["image/*"]}]}})
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(rules, TIKTOK, "input[type=file]", _on_page(_tiktok_input()))


def test_a_hidden_input_with_no_accept_attribute_is_refused():
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_ACCEPT_UPLOAD_RULES, TIKTOK, "input[type=file]",
                               _on_page(_file_input(style="display: none;")))


def test_accept_does_not_admit_a_disabled_readonly_or_decoy_hidden_input():
    for extra in ({"disabled": ""}, {"readonly": ""}, {"data-agent-action": "upload"}):
        with pytest.raises(ValidationError, match="not a file input"):
            validate_upload_target(_ACCEPT_UPLOAD_RULES, TIKTOK, "input[type=file]",
                                   _on_page(_tiktok_input(**extra)))


def test_accept_still_needs_exactly_one_selector_match():
    with pytest.raises(ValidationError, match="ambiguous"):
        validate_upload_target(_ACCEPT_UPLOAD_RULES, TIKTOK, "input[type=file]",
                               {**_found(_tiktok_input(), _tiktok_input()),
                                "survey": [_tiktok_input(), _tiktok_input()]})


def test_accept_refuses_when_a_second_input_on_the_page_carries_the_same_value():
    """accept names a file type, not a control: the agent's selector may pick
    one of two same-typed inputs (a post attachment beside a DM one), so the
    value must name exactly one input on the page."""
    for peer in (_tiktok_input(), _file_input(accept="video/*")):
        with pytest.raises(ValidationError, match="not a file input"):
            validate_upload_target(_ACCEPT_UPLOAD_RULES, TIKTOK, "input[accept]",
                                   _on_page(_tiktok_input(), peer))


def test_accept_uniqueness_ignores_inputs_it_could_not_reach_anyway():
    """Peers with other values, or with an id/name (field_ids' to name), don't count."""
    assert validate_upload_target(
        _ACCEPT_UPLOAD_RULES, TIKTOK, "input[accept]",
        _on_page(_tiktok_input(), _file_input(accept="image/*"),
                 _file_input(node_id="cover", accept="video/*"),
                 _file_input(name="thumb", accept="video/*"))) is True


def test_accept_without_a_survey_admits_nothing():
    """A caller that took no page survey cannot prove uniqueness: fail closed."""
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_ACCEPT_UPLOAD_RULES, TIKTOK, "input[type=file]",
                               _found(_tiktok_input()))


def test_accept_never_reaches_an_input_that_has_an_id_or_name():
    """An input with an id/name is field_ids' to name; one left out of field_ids
    is not admitted because it happens to take the same file type."""
    rules = BrowdenAccessRuleSet({"upload-file": {"x.test": [{
        "path": [".*"], "label": "(?!)", "hidden_input": True,
        "field_ids": ["avatar"], "accept": ["image/*"]}]}})
    url = "https://x.test/post"
    for named in (_file_input(node_id="post-attach", accept="image/*", style="display:none"),
                  _file_input(name="post-attach", accept="image/*", style="display:none")):
        with pytest.raises(ValidationError, match="not a file input"):
            validate_upload_target(rules, url, "input", _on_page(named))
    # ...while the field it does list is still admitted by its id.
    assert validate_upload_target(rules, url, "input", _on_page(
        _file_input(node_id="avatar", accept="image/*", style="display:none"))) is True


def test_a_truncated_accept_value_never_matches():
    """The serializer caps attribute values; a prefix is not the value."""
    node = _tiktok_input()
    node["attributes_truncated"] = ["accept"]
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(_ACCEPT_UPLOAD_RULES, TIKTOK, "input", _on_page(node))


def test_hidden_input_and_accept_must_be_in_the_same_rule():
    """hidden_input in one rule and accept in another on the same URL admit nothing hidden."""
    rules = BrowdenAccessRuleSet({"upload-file": {"www.tiktok.com": [
        {"path": [".*"], "label": "(?!)", "hidden_input": True, "field_ids": ["other"]},
        {"path": [".*"], "label": "(?!)", "accept": ["video/*"]}]}})
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(rules, TIKTOK, "input", _on_page(_tiktok_input()))


def test_accept_compares_exactly_modulo_whitespace_around_commas():
    rules = BrowdenAccessRuleSet({"upload-file": {"x.test": [{
        "path": [".*"], "label": "(?!)", "hidden_input": True, "accept": ["image/*, .pdf"]}]}})
    url = "https://x.test/form"
    assert validate_upload_target(rules, url, "input", _on_page(_file_input(
        accept=" image/* ,.pdf", style="display:none"))) is True
    for other in ("image/*", ".pdf,image/*", "IMAGE/*,.pdf", "image/.*,.pdf"):
        with pytest.raises(ValidationError, match="not a file input"):
            validate_upload_target(rules, url, "input", _on_page(_file_input(
                accept=other, style="display:none")))


def test_gate_3_authorizes_a_visible_label_less_input_by_accept():
    """Without hidden_input, accept is gate 3's id half for a visible input."""
    rules = BrowdenAccessRuleSet({"upload-file": {"x.test": [{
        "path": [".*"], "label": "(?!)", "accept": ["video/*"]}]}})
    url = "https://x.test/upload"
    assert validate_upload_target(rules, url, "input",
                                  _on_page(_file_input(accept="video/*"))) is False
    with pytest.raises(ValidationError, match="does not match any upload-file rule"):
        validate_upload_target(rules, url, "input", _on_page(_file_input(accept="image/*")))
    # ...the uniqueness rule holds here too...
    with pytest.raises(ValidationError, match="does not match any upload-file rule"):
        validate_upload_target(rules, url, "input", _on_page(
            _file_input(accept="video/*"), _file_input(accept="video/*")))
    # ...and without hidden_input it does not reach a hidden input.
    with pytest.raises(ValidationError, match="not a file input"):
        validate_upload_target(rules, url, "input", _on_page(_tiktok_input()))


def test_the_gate_records_an_accept_admission_as_hidden(tmp_path):
    location = tmp_path / "videos"
    location.mkdir()
    (location / "clip.mp4").write_bytes(b"mp4")
    rules = BrowdenAccessRuleSet({
        "upload-file": {"www.tiktok.com": [{
            "path": ["^/tiktokstudio/upload$"], "label": ".*", "hidden_input": True,
            "accept": ["video/*"]}]},
        "allowed_upload_locations": [str(location)],
    })
    gate = upload_file_gate(rules, str(location / "clip.mp4"))
    gate.check_page(TIKTOK)
    gate.check_element(TIKTOK, "input[type=file]", _on_page(_tiktok_input()))
    assert gate.control.hidden is True


# -- accept + the size cap: an accept admission is capped by the rules admitting it

def _tiktok_cap_rules(location):
    """The live TikTok rule, with its own 100 MiB cap."""
    return BrowdenAccessRuleSet({
        "upload-file": {"www.tiktok.com": [
            {"path": ["^/tiktokstudio/upload$"], "label": ".*", "hidden_input": True,
             "accept": ["video/*"], "max_bytes": 100 * MIB}]},
        "allowed_upload_locations": [str(location)],
    })


def _run_tiktok_gate(rules, path, found):
    gate = upload_file_gate(rules, str(path))
    gate.check_page(TIKTOK)
    gate.check_element(TIKTOK, "input[type=file]", found)
    return gate


def test_an_accept_rule_admits_a_file_under_its_own_max_bytes(allowed):
    rules = _tiktok_cap_rules(allowed[0])
    gate = _run_tiktok_gate(rules, _sparse(allowed[0] / "clip.mp4", 72 * MIB),
                            _on_page(_tiktok_input()))
    assert gate.control.hidden is True
    _run_tiktok_gate(rules, _sparse(allowed[0] / "edge.mp4", 100 * MIB), _on_page(_tiktok_input()))


def test_an_accept_rule_refuses_a_file_over_its_own_max_bytes(allowed):
    rules = _tiktok_cap_rules(allowed[0])
    with pytest.raises(ValidationError, match=rf"over the {100 * MIB}-byte upload cap"):
        _run_tiktok_gate(rules, _sparse(allowed[0] / "long.mp4", 100 * MIB + 1),
                         _on_page(_tiktok_input()))


def test_an_accept_rules_cap_is_not_lent_to_a_control_it_does_not_admit(allowed):
    """The 100 MiB accept rule sits on the page, but the avatar input is admitted
    only by a field_ids rule with the 1 MiB default: that is its cap. (The accept
    rule's label matches nothing here: the live rule's '.*' would admit any
    visible input by label, and so lend it the cap by design.)"""
    rules = BrowdenAccessRuleSet({
        "upload-file": {"www.tiktok.com": [
            {"path": ["^/tiktokstudio/upload$"], "label": "(?!)", "hidden_input": True,
             "accept": ["video/*"], "max_bytes": 100 * MIB},
            {"path": ["^/tiktokstudio/upload$"], "label": "(?!)", "field_ids": ["avatar"]}]},
        "allowed_upload_locations": [str(allowed[0])],
    })
    with pytest.raises(ValidationError, match=rf"over the {MIB}-byte upload cap"):
        _run_tiktok_gate(rules, _sparse(allowed[0] / "big.png", 2 * MIB),
                         _on_page(_file_input(node_id="avatar"), _tiktok_input()))


def test_an_accept_admission_refused_for_a_peer_is_not_saved_by_the_cap(allowed):
    """A second id-less video input still refuses, whatever the file's size."""
    rules = _tiktok_cap_rules(allowed[0])
    with pytest.raises(ValidationError, match="not a file input"):
        _run_tiktok_gate(rules, _sparse(allowed[0] / "small.mp4", 10),
                         _on_page(_tiktok_input(), _tiktok_input()))
