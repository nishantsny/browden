import pytest

from browden.mcp.validator import BrowdenRuntimeConfiguration
from browden.mcp.validator.runtime_configuration import (
    DEFAULT_MAX_BROWSER_SESSIONS,
    DEFAULT_MAX_TABS_PER_SESSION,
    DEFAULT_REAP_INTERVAL_SECONDS,
)


@pytest.fixture
def rc() -> BrowdenRuntimeConfiguration:
    return BrowdenRuntimeConfiguration(
        {
            "read": {"website_overrides": {"*": [".*"]}},
            "click": {
                "amazon.com": {"paths": [".*"], "label": "(?i)\\badd to cart\\b"},
                "amazon.in": {"paths": ["^/dp/.*"], "label": "(?i)\\badd to cart\\b"},
                "ebay.com": {"paths": [".*"], "label": "(?i)\\badd to (cart|basket)\\b"},
            },
        }
    )


# -- read policy: overrides ---------------------------------------------------

def test_read_overrides_wildcard_is_wide_open(rc):
    assert rc.read_policy.is_allowed("anything.example.com", "/whatever")


def test_read_overrides_can_path_scope():
    rc = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"example.com": ["^/docs/.*"]}}})
    assert rc.read_policy.is_allowed("example.com", "/docs/intro")
    assert not rc.read_policy.is_allowed("example.com", "/secret")
    assert not rc.read_policy.is_allowed("other.com", "/docs/intro")


def test_path_regex_must_match_whole_path():
    # A path rule fullmatches: `^/products` alone covers only exactly `/products`,
    # not a sibling like `/products-secret-admin`. A prefix is spelled `.*`.
    rc = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"example.com": ["^/products"]}}})
    assert rc.read_policy.is_allowed("example.com", "/products")
    assert not rc.read_policy.is_allowed("example.com", "/products-secret-admin")
    assert not rc.read_policy.is_allowed("example.com", "/products/42")  # needs ^/products/.*
    wide = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"example.com": ["^/products/.*"]}}})
    assert wide.read_policy.is_allowed("example.com", "/products/42")


def test_read_default_denies_when_nothing_listed():
    # No read block and no overrides => fail closed.
    assert not BrowdenRuntimeConfiguration({}).read_policy.is_allowed("example.com", "/")


# -- read policy: Tranco ------------------------------------------------------

def test_read_tranco_allows_top_sites_and_subdomains():
    rc = BrowdenRuntimeConfiguration({"read": {"tranco": {"enabled": True, "top_n": 1000}}})
    # google.com is Tranco rank #1 — in any top_n.
    assert rc.read_policy.is_allowed("google.com", "/")
    assert rc.read_policy.is_allowed("mail.google.com", "/inbox")  # subdomain covered
    assert rc.read_policy.is_allowed("www.google.com", "/")        # www stripped
    # A lookalike must not ride on the listed domain.
    assert not rc.read_policy.is_allowed("google.com.evil.co", "/")
    # A domain that is nowhere near the top is denied.
    assert not rc.read_policy.is_allowed("nonexistent-xyz-987654.test", "/")


def test_read_tranco_top_n_is_a_cutoff():
    # top_n=1 keeps only rank #1 (google.com); rank #2 (cloudflare.com) is out.
    rc = BrowdenRuntimeConfiguration({"read": {"tranco": {"enabled": True, "top_n": 1}}})
    assert rc.read_policy.is_allowed("google.com", "/")
    assert not rc.read_policy.is_allowed("cloudflare.com", "/")


def test_read_tranco_disabled_by_default():
    # tranco present but enabled:false => contributes no allows.
    rc = BrowdenRuntimeConfiguration({"read": {"tranco": {"enabled": False}}})
    assert not rc.read_policy.is_allowed("google.com", "/")


def test_read_tranco_and_overrides_are_both_honored():
    rc = BrowdenRuntimeConfiguration({"read": {
        "tranco": {"enabled": True, "top_n": 100},
        "website_overrides": {"intranet.corp": [".*"]},
    }})
    assert rc.read_policy.is_allowed("google.com", "/")        # via Tranco
    assert rc.read_policy.is_allowed("intranet.corp", "/wiki")  # via overrides


def test_override_triumphs_over_tranco_and_can_restrict():
    # google.com is in Tranco (blanket allow), but an explicit override governs
    # it — so it is path-scoped, and Tranco no longer waves the rest through.
    rc = BrowdenRuntimeConfiguration({"read": {
        "tranco": {"enabled": True, "top_n": 1000},
        "website_overrides": {"google.com": ["^/allowed/.*"]},
    }})
    assert rc.read_policy.is_allowed("google.com", "/allowed/x")
    assert not rc.read_policy.is_allowed("google.com", "/other")   # override restricts
    # A Tranco host WITHOUT an override still rides on Tranco.
    assert rc.read_policy.is_allowed("cloudflare.com", "/anything")


def test_wildcard_override_scopes_every_host_over_tranco():
    rc = BrowdenRuntimeConfiguration({"read": {
        "tranco": {"enabled": True, "top_n": 1000},
        "website_overrides": {"*": ["^/docs/.*"]},
    }})
    assert rc.read_policy.is_allowed("google.com", "/docs/x")  # top site, path matches
    assert not rc.read_policy.is_allowed("google.com", "/home")  # scoped even for a top site


def test_denylist_still_beats_a_triumphing_override():
    rc = BrowdenRuntimeConfiguration({
        "read": {"website_overrides": {"*": [".*"]}},
        "denylist": {"evil.test": [".*"]},
    })
    assert rc.read_policy.is_allowed("anything.test", "/")   # overrides open the web
    assert not rc.read_policy.is_allowed("evil.test", "/")   # denylist still wins


# -- read policy: master switch ----------------------------------------------

def test_read_disabled_allows_everything_non_denied():
    rc = BrowdenRuntimeConfiguration({"read": {"enabled": False}})
    assert rc.read_policy.is_allowed("literally-anything.test", "/x")


# -- denylist -----------------------------------------------------------------

def test_denylist_vetoes_even_tranco():
    rc = BrowdenRuntimeConfiguration({
        "read": {"tranco": {"enabled": True, "top_n": 1000}},
        "denylist": {"google.com": [".*"]},
    })
    assert not rc.read_policy.is_allowed("google.com", "/")   # denied despite Tranco
    assert rc.read_policy.is_allowed("cloudflare.com", "/")   # other top sites unaffected


def test_denylist_vetoes_even_when_read_disabled():
    rc = BrowdenRuntimeConfiguration({
        "read": {"enabled": False},
        "denylist": {"evil.test": [".*"]},
    })
    assert not rc.read_policy.is_allowed("evil.test", "/")
    assert rc.read_policy.is_allowed("anything-else.test", "/")


def test_denylist_can_path_scope_and_is_queryable():
    rc = BrowdenRuntimeConfiguration({"denylist": {"example.com": ["^/checkout"]}})
    assert rc.is_denied("example.com", "/checkout/pay")
    assert not rc.is_denied("example.com", "/browse")
    assert not rc.is_denied("other.com", "/checkout")


def test_www_prefixed_rule_keys_are_canonicalized():
    # A "www."-prefixed rule key must not be silently inert: lookups strip www.,
    # so the key is canonicalized to match. A denylist {www.tracker.com} blocks
    # both www.tracker.com and the bare apex, and matches however you query it.
    rc = BrowdenRuntimeConfiguration({"denylist": {"www.tracker.com": [".*"]}})
    assert rc.is_denied("www.tracker.com", "/")
    assert rc.is_denied("tracker.com", "/")
    # Same for a www.-keyed read override and write-action host.
    al2 = BrowdenRuntimeConfiguration({
        "read": {"website_overrides": {"www.intranet.corp": [".*"]}},
        "click": {"www.shop.test": {"paths": [".*"], "label": ".*"}},
    })
    assert al2.read_policy.is_allowed("intranet.corp", "/wiki")
    assert al2.section("click").is_allowed("shop.test", "/cart")


def test_empty_denylist_denies_nothing():
    assert not BrowdenRuntimeConfiguration({"denylist": {}}).is_denied("anywhere.test", "/x")


# -- M1: path normalization (dot-segments / %2e can't evade path rules) --------

def test_denylist_not_evaded_by_dot_segments():
    # Chrome resolves ./ , /../ and %2e before requesting, so a path-scoped
    # denylist must decide on the same normalized form it will actually fetch.
    rc = BrowdenRuntimeConfiguration({"denylist": {"reddit.com": ["^/checkout"]}})
    assert rc.is_denied("reddit.com", "/checkout")
    assert rc.is_denied("reddit.com", "/./checkout")      # was a bypass
    assert rc.is_denied("reddit.com", "/x/../checkout")   # was a bypass
    assert rc.is_denied("reddit.com", "/%2e/checkout")    # was a bypass
    assert rc.is_denied("reddit.com", "/%2E/checkout")    # case-insensitive


def test_path_scoped_allow_not_escaped_by_dot_segments():
    rc = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"example.com": ["^/docs/.*"]}}})
    assert rc.read_policy.is_allowed("example.com", "/docs/x/../intro")  # stays /docs/
    assert not rc.read_policy.is_allowed("example.com", "/docs/../secret")  # escapes /docs/


def test_normalization_preserves_trailing_slash():
    rc = BrowdenRuntimeConfiguration({"denylist": {"example.com": ["^/checkout/$"]}})
    assert rc.is_denied("example.com", "/checkout/")
    assert rc.is_denied("example.com", "/./checkout/")


# -- M2: override_has_host (the scheme gate's opt-in check) -------------------

def test_override_has_host_is_explicit_not_wildcard():
    rc = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"localhost": [".*"], "*": [".*"]}}})
    rp = rc.read_policy
    assert rp.override_has_host("localhost")          # explicit entry
    assert rp.override_has_host("www.localhost")      # www-canonicalized
    assert not rp.override_has_host("example.com")    # only "*" covers it — not explicit
    assert not rp.override_has_host("")               # no empty-host entry here


def test_override_has_host_matches_empty_host_for_file():
    rp = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"": ["^/home/.*"]}}}).read_policy
    assert rp.override_has_host("")                    # the authority-less file:// host
    assert not rp.override_has_host("example.com")


# -- write (click) section ----------------------------------------------------

def test_click_allows_listed_hosts(rc):
    assert rc.section("click").is_allowed("amazon.com", "/dp/B0FBRRM2VQ")
    assert rc.section("click").is_allowed("www.amazon.com", "/")  # www stripped


def test_click_denies_unlisted_host(rc):
    assert not rc.section("click").is_allowed("evil.example.com", "/")
    # No wildcard fallback in the write section.
    assert not rc.section("click").is_allowed("amazon.de", "/")


def test_click_respects_per_host_paths(rc):
    assert not rc.section("click").is_allowed("amazon.in", "/gp/cart")  # only /dp/*
    assert rc.section("click").is_allowed("amazon.in", "/dp/B0FBRRM2VQ")


def test_unknown_action_default_denies(rc):
    assert not rc.section("delete_account").is_allowed("amazon.com", "/")


def test_rules_for_lookup(rc):
    rules = rc.rules_for("click", "amazon.com", "/dp/x")
    assert len(rules) == 1 and rules[0].label.search("Add to Cart")
    assert rc.rules_for("click", "www.amazon.com", "/")           # canonicalized
    assert rc.rules_for("click", "evil.example.com", "/") == []
    assert rc.rules_for("read", "amazon.com", "/") == []          # read is not a write section
    # amazon.in only scopes /dp/* — a non-matching page yields no rules.
    assert rc.rules_for("click", "amazon.in", "/gp/cart") == []
    assert rc.rules_for("click", "amazon.in", "/dp/x")


def test_ebay_label_allows_basket(rc):
    (rule,) = rc.rules_for("click", "ebay.com", "/anything")
    assert rule.label.search("Add to basket") and rule.label.search("Add to cart")


# -- YAML loading -------------------------------------------------------------

def test_from_file_parses_yaml(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        "  tranco: {enabled: true, top_n: 1000}\n"
        "  website_overrides:\n"
        '    "*": [".*"]\n'
        "click:\n"
        "  amazon.com:\n"
        '    paths: [".*"]\n'
        "    label: '(?i)\\badd to cart\\b'\n"
    )
    rc = BrowdenRuntimeConfiguration.from_file(f)
    assert rc.read_policy.is_allowed("anything.example.com", "/whatever")  # via overrides
    assert rc.read_policy.is_allowed("google.com", "/")                    # via Tranco
    assert rc.section("click").is_allowed("www.amazon.com", "/dp/X")
    (rule,) = rc.rules_for("click", "amazon.com", "/dp/X")
    assert rule.label.search("Add to Cart") and not rule.label.search("Buy Now")


def test_from_file_all_comments_is_deny_all(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("# everything commented out\n# read:\n#   enabled: true\n")
    rc = BrowdenRuntimeConfiguration.from_file(f)
    assert not rc.read_policy.is_allowed("example.com", "/")
    assert not rc.section("click").is_allowed("amazon.com", "/")


# -- host canonicalization consistency (H3) ----------------------------------

def test_denylist_not_bypassed_by_trailing_dot():
    # evil.com. resolves to evil.com in the browser, so the denylist must treat
    # them alike (canonical_host strips the trailing dot) or the deny is bypassed.
    rc = BrowdenRuntimeConfiguration({
        "read": {"tranco": {"enabled": True, "top_n": 1000}},
        "denylist": {"google.com": [".*"]},
    })
    assert rc.is_denied("google.com", "/")
    assert rc.is_denied("google.com.", "/")                    # trailing-dot form
    assert not rc.read_policy.is_allowed("google.com.", "/")   # net: still blocked


def test_denylist_keys_and_lookups_agree_on_www():
    # A rule written with or without www. matches a host written either way —
    # keys and lookups canonicalize identically, so no silently-inert entry.
    for entry in ("tracker.com", "www.tracker.com"):
        rc = BrowdenRuntimeConfiguration({"denylist": {entry: [".*"]}})
        assert rc.is_denied("tracker.com", "/")
        assert rc.is_denied("www.tracker.com", "/")


def test_override_matches_trailing_dot_and_www():
    rc = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"example.com": [".*"]}}})
    assert rc.read_policy.is_allowed("example.com", "/")
    assert rc.read_policy.is_allowed("example.com.", "/")
    assert rc.read_policy.is_allowed("www.example.com", "/")


# -- page-scoped rules (write) ------------------------------------------------

def test_write_label_is_scoped_per_page():
    # amazon.com authorizes different controls on different pages: "add to cart"
    # on a product page, "place your order" only in the checkout pipeline.
    rc = BrowdenRuntimeConfiguration({"click": {"amazon.com": [
        {"path": ["^/(dp|gp/product)/.*"], "label": "(?i)add to cart"},
        {"path": ["^/gp/buy/.*"], "label": "(?i)place your order"},
    ]}})

    def labels(path):
        return {r.label.pattern for r in rc.rules_for("click", "amazon.com", path)}

    assert labels("/dp/B0X") == {"(?i)add to cart"}
    assert labels("/gp/buy/spc") == {"(?i)place your order"}
    # A page matched by no rule authorizes nothing — the whole point.
    assert labels("/gp/css/account/close") == set()


def test_field_ids_are_page_scoped():
    # A label-less field id is typable only on the page whose rule lists it.
    rc = BrowdenRuntimeConfiguration({"write-text": {"amazon.com": [
        {"path": ["^/gp/css/order-history.*"], "label": "(?i)tip",
         "field_ids": ["tip-input"]},
    ]}})
    (rule,) = rc.rules_for("write-text", "amazon.com", "/gp/css/order-history")
    assert rule.field_ids == frozenset({"tip-input"})
    assert rc.rules_for("write-text", "amazon.com", "/dp/B0X") == []  # no id here


def test_match_on_url_scopes_a_hash_router_spa():
    # Every SPA page shares path "/"; only match_on: url can tell them apart.
    rc = BrowdenRuntimeConfiguration({"click": {"secure.splitwise.com": [
        {"path": [r"^/#/friends/\d+$"], "match_on": "url", "label": "(?i)save"},
    ]}})
    friend = rc.rules_for("click", "secure.splitwise.com", "/", fragment="/friends/42")
    assert len(friend) == 1
    # The group page (same path "/") is NOT authorized.
    assert rc.rules_for("click", "secure.splitwise.com", "/", fragment="/groups/7") == []
    # A path-only match (fragment dropped) would wrongly see them as identical.
    assert rc.rules_for("click", "secure.splitwise.com", "/") == []


def test_match_on_url_read_override_scopes_spa_pages():
    rc = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"app.example.com": [
        {"path": [r"^/#/reports/\d+$"], "match_on": "url"},
    ]}}})
    assert rc.read_policy.is_allowed("app.example.com", "/", fragment="/reports/9")
    assert not rc.read_policy.is_allowed("app.example.com", "/", fragment="/admin")
    # Listing the host demotes it from any Tranco grant to just these pages.
    assert not rc.read_policy.is_allowed("app.example.com", "/other")


def test_page_rule_requires_label_for_write_actions():
    with pytest.raises(ValueError, match="requires a 'label'"):
        BrowdenRuntimeConfiguration({"click": {"amazon.com": [{"path": ["^/dp/.*"]}]}})


def test_page_rule_rejects_label_on_read_override():
    with pytest.raises(ValueError, match="only meaningful for a write action"):
        BrowdenRuntimeConfiguration({"read": {"website_overrides": {
            "x.com": [{"path": [".*"], "label": ".*"}]}}})


def test_page_rule_rejects_unknown_match_on():
    with pytest.raises(ValueError, match="match_on must be"):
        BrowdenRuntimeConfiguration({"click": {"amazon.com": [
            {"path": [".*"], "match_on": "host", "label": ".*"}]}})


def test_legacy_and_page_rule_forms_coexist():
    # Legacy host-wide dict and the new page-rule list load side by side.
    rc = BrowdenRuntimeConfiguration({"click": {
        "ebay.com": {"paths": [".*"], "label": "(?i)add"},           # legacy
        "amazon.com": [{"path": ["^/dp/.*"], "label": "(?i)add"}],   # page rules
    }})
    assert rc.rules_for("click", "ebay.com", "/anything")
    assert rc.rules_for("click", "amazon.com", "/dp/x")
    assert rc.rules_for("click", "amazon.com", "/other") == []


# -- infra knobs -------------------------------------------------------------

def test_infra_defaults_when_the_section_is_absent():
    rc = BrowdenRuntimeConfiguration({})
    assert rc.max_browser_sessions == DEFAULT_MAX_BROWSER_SESSIONS
    assert rc.max_tabs_per_session == DEFAULT_MAX_TABS_PER_SESSION
    assert rc.reap_interval_seconds == DEFAULT_REAP_INTERVAL_SECONDS == 7200


def test_infra_reap_interval_is_read_from_the_config():
    rc = BrowdenRuntimeConfiguration({"infra": {"reap_interval_seconds": 600}})
    assert rc.reap_interval_seconds == 600
    # An unset sibling keeps its default rather than following the one that was set.
    assert rc.max_tabs_per_session == DEFAULT_MAX_TABS_PER_SESSION


# The shipped sample (configs/samples/read_only_on_popular_websites.yaml) is
# covered by test/unit/configs/test_loader.py through the schema-validating loader.
