from pathlib import Path

import pytest

from browden.mcp.validator import ActionAllowlist, PolicySet
from browden.mcp.validator.allowlist import (
    DEFAULT_MAX_BROWSER_SESSIONS,
    DEFAULT_MAX_TABS_PER_SESSION,
    DEFAULT_REAP_INTERVAL_SECONDS,
)


@pytest.fixture
def al() -> ActionAllowlist:
    return ActionAllowlist(
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

def test_read_overrides_wildcard_is_wide_open(al):
    assert al.policy.read_policy.is_allowed("anything.example.com", "/whatever")


def test_read_overrides_can_path_scope():
    al = ActionAllowlist({"read": {"website_overrides": {"example.com": ["^/docs/.*"]}}})
    assert al.policy.read_policy.is_allowed("example.com", "/docs/intro")
    assert not al.policy.read_policy.is_allowed("example.com", "/secret")
    assert not al.policy.read_policy.is_allowed("other.com", "/docs/intro")


def test_path_regex_must_match_whole_path():
    # A path rule fullmatches: `^/products` alone covers only exactly `/products`,
    # not a sibling like `/products-secret-admin`. A prefix is spelled `.*`.
    al = ActionAllowlist({"read": {"website_overrides": {"example.com": ["^/products"]}}})
    assert al.policy.read_policy.is_allowed("example.com", "/products")
    assert not al.policy.read_policy.is_allowed("example.com", "/products-secret-admin")
    assert not al.policy.read_policy.is_allowed("example.com", "/products/42")  # needs ^/products/.*
    wide = ActionAllowlist({"read": {"website_overrides": {"example.com": ["^/products/.*"]}}})
    assert wide.policy.read_policy.is_allowed("example.com", "/products/42")


def test_read_default_denies_when_nothing_listed():
    # No read block and no overrides => fail closed.
    assert not ActionAllowlist({}).policy.read_policy.is_allowed("example.com", "/")


# -- read policy: Tranco ------------------------------------------------------

def test_read_tranco_allows_top_sites_and_subdomains():
    al = ActionAllowlist({"read": {"tranco": {"enabled": True, "top_n": 1000}}})
    # google.com is Tranco rank #1 — in any top_n.
    assert al.policy.read_policy.is_allowed("google.com", "/")
    assert al.policy.read_policy.is_allowed("mail.google.com", "/inbox")  # subdomain covered
    assert al.policy.read_policy.is_allowed("www.google.com", "/")        # www stripped
    # A lookalike must not ride on the listed domain.
    assert not al.policy.read_policy.is_allowed("google.com.evil.co", "/")
    # A domain that is nowhere near the top is denied.
    assert not al.policy.read_policy.is_allowed("nonexistent-xyz-987654.test", "/")


def test_read_tranco_top_n_is_a_cutoff():
    # top_n=1 keeps only rank #1 (google.com); rank #2 (cloudflare.com) is out.
    al = ActionAllowlist({"read": {"tranco": {"enabled": True, "top_n": 1}}})
    assert al.policy.read_policy.is_allowed("google.com", "/")
    assert not al.policy.read_policy.is_allowed("cloudflare.com", "/")


def test_read_tranco_disabled_by_default():
    # tranco present but enabled:false => contributes no allows.
    al = ActionAllowlist({"read": {"tranco": {"enabled": False}}})
    assert not al.policy.read_policy.is_allowed("google.com", "/")


def test_read_tranco_and_overrides_are_both_honored():
    al = ActionAllowlist({"read": {
        "tranco": {"enabled": True, "top_n": 100},
        "website_overrides": {"intranet.corp": [".*"]},
    }})
    assert al.policy.read_policy.is_allowed("google.com", "/")        # via Tranco
    assert al.policy.read_policy.is_allowed("intranet.corp", "/wiki")  # via overrides


def test_override_triumphs_over_tranco_and_can_restrict():
    # google.com is in Tranco (blanket allow), but an explicit override governs
    # it — so it is path-scoped, and Tranco no longer waves the rest through.
    al = ActionAllowlist({"read": {
        "tranco": {"enabled": True, "top_n": 1000},
        "website_overrides": {"google.com": ["^/allowed/.*"]},
    }})
    assert al.policy.read_policy.is_allowed("google.com", "/allowed/x")
    assert not al.policy.read_policy.is_allowed("google.com", "/other")   # override restricts
    # A Tranco host WITHOUT an override still rides on Tranco.
    assert al.policy.read_policy.is_allowed("cloudflare.com", "/anything")


def test_wildcard_override_scopes_every_host_over_tranco():
    al = ActionAllowlist({"read": {
        "tranco": {"enabled": True, "top_n": 1000},
        "website_overrides": {"*": ["^/docs/.*"]},
    }})
    assert al.policy.read_policy.is_allowed("google.com", "/docs/x")  # top site, path matches
    assert not al.policy.read_policy.is_allowed("google.com", "/home")  # scoped even for a top site


def test_denylist_still_beats_a_triumphing_override():
    al = ActionAllowlist({
        "read": {"website_overrides": {"*": [".*"]}},
        "denylist": {"evil.test": [".*"]},
    })
    assert al.policy.read_policy.is_allowed("anything.test", "/")   # overrides open the web
    assert not al.policy.read_policy.is_allowed("evil.test", "/")   # denylist still wins


# -- read policy: master switch ----------------------------------------------

def test_read_disabled_allows_everything_non_denied():
    al = ActionAllowlist({"read": {"enabled": False}})
    assert al.policy.read_policy.is_allowed("literally-anything.test", "/x")


# -- denylist -----------------------------------------------------------------

def test_denylist_vetoes_even_tranco():
    al = ActionAllowlist({
        "read": {"tranco": {"enabled": True, "top_n": 1000}},
        "denylist": {"google.com": [".*"]},
    })
    assert not al.policy.read_policy.is_allowed("google.com", "/")   # denied despite Tranco
    assert al.policy.read_policy.is_allowed("cloudflare.com", "/")   # other top sites unaffected


def test_denylist_vetoes_even_when_read_disabled():
    al = ActionAllowlist({
        "read": {"enabled": False},
        "denylist": {"evil.test": [".*"]},
    })
    assert not al.policy.read_policy.is_allowed("evil.test", "/")
    assert al.policy.read_policy.is_allowed("anything-else.test", "/")


def test_denylist_can_path_scope_and_is_queryable():
    al = ActionAllowlist({"denylist": {"example.com": ["^/checkout"]}})
    assert al.policy.is_denied("example.com", "/checkout/pay")
    assert not al.policy.is_denied("example.com", "/browse")
    assert not al.policy.is_denied("other.com", "/checkout")


def test_www_prefixed_rule_keys_are_canonicalized():
    # A "www."-prefixed rule key must not be silently inert: lookups strip www.,
    # so the key is canonicalized to match. A denylist {www.tracker.com} blocks
    # both www.tracker.com and the bare apex, and matches however you query it.
    al = ActionAllowlist({"denylist": {"www.tracker.com": [".*"]}})
    assert al.policy.is_denied("www.tracker.com", "/")
    assert al.policy.is_denied("tracker.com", "/")
    # Same for a www.-keyed read override and write-action host.
    al2 = ActionAllowlist({
        "read": {"website_overrides": {"www.intranet.corp": [".*"]}},
        "click": {"www.shop.test": {"paths": [".*"], "label": ".*"}},
    })
    assert al2.policy.read_policy.is_allowed("intranet.corp", "/wiki")
    assert al2.policy.section("click").is_allowed("shop.test", "/cart")


def test_empty_denylist_denies_nothing():
    assert not ActionAllowlist({"denylist": {}}).policy.is_denied("anywhere.test", "/x")


# -- M1: path normalization (dot-segments / %2e can't evade path rules) --------

def test_denylist_not_evaded_by_dot_segments():
    # Chrome resolves ./ , /../ and %2e before requesting, so a path-scoped
    # denylist must decide on the same normalized form it will actually fetch.
    al = ActionAllowlist({"denylist": {"reddit.com": ["^/checkout"]}})
    assert al.policy.is_denied("reddit.com", "/checkout")
    assert al.policy.is_denied("reddit.com", "/./checkout")      # was a bypass
    assert al.policy.is_denied("reddit.com", "/x/../checkout")   # was a bypass
    assert al.policy.is_denied("reddit.com", "/%2e/checkout")    # was a bypass
    assert al.policy.is_denied("reddit.com", "/%2E/checkout")    # case-insensitive


def test_path_scoped_allow_not_escaped_by_dot_segments():
    al = ActionAllowlist({"read": {"website_overrides": {"example.com": ["^/docs/.*"]}}})
    assert al.policy.read_policy.is_allowed("example.com", "/docs/x/../intro")  # stays /docs/
    assert not al.policy.read_policy.is_allowed("example.com", "/docs/../secret")  # escapes /docs/


def test_normalization_preserves_trailing_slash():
    al = ActionAllowlist({"denylist": {"example.com": ["^/checkout/$"]}})
    assert al.policy.is_denied("example.com", "/checkout/")
    assert al.policy.is_denied("example.com", "/./checkout/")


# -- M2: override_has_host (the scheme gate's opt-in check) -------------------

def test_override_has_host_is_explicit_not_wildcard():
    al = ActionAllowlist({"read": {"website_overrides": {"localhost": [".*"], "*": [".*"]}}})
    rp = al.policy.read_policy
    assert rp.override_has_host("localhost")          # explicit entry
    assert rp.override_has_host("www.localhost")      # www-canonicalized
    assert not rp.override_has_host("example.com")    # only "*" covers it — not explicit
    assert not rp.override_has_host("")               # no empty-host entry here


def test_override_has_host_matches_empty_host_for_file():
    rp = ActionAllowlist({"read": {"website_overrides": {"": ["^/home/.*"]}}}).policy.read_policy
    assert rp.override_has_host("")                    # the authority-less file:// host
    assert not rp.override_has_host("example.com")


# -- write (click) section ----------------------------------------------------

def test_click_allows_listed_hosts(al):
    assert al.policy.section("click").is_allowed("amazon.com", "/dp/B0FBRRM2VQ")
    assert al.policy.section("click").is_allowed("www.amazon.com", "/")  # www stripped


def test_click_denies_unlisted_host(al):
    assert not al.policy.section("click").is_allowed("evil.example.com", "/")
    # No wildcard fallback in the write section.
    assert not al.policy.section("click").is_allowed("amazon.de", "/")


def test_click_respects_per_host_paths(al):
    assert not al.policy.section("click").is_allowed("amazon.in", "/gp/cart")  # only /dp/*
    assert al.policy.section("click").is_allowed("amazon.in", "/dp/B0FBRRM2VQ")


def test_unknown_action_default_denies(al):
    assert not al.policy.section("delete_account").is_allowed("amazon.com", "/")


def test_rules_for_lookup(al):
    rules = al.policy.rules_for("click", "amazon.com", "/dp/x")
    assert len(rules) == 1 and rules[0].label.search("Add to Cart")
    assert al.policy.rules_for("click", "www.amazon.com", "/")           # canonicalized
    assert al.policy.rules_for("click", "evil.example.com", "/") == []
    assert al.policy.rules_for("read", "amazon.com", "/") == []          # read is not a write section
    # amazon.in only scopes /dp/* — a non-matching page yields no rules.
    assert al.policy.rules_for("click", "amazon.in", "/gp/cart") == []
    assert al.policy.rules_for("click", "amazon.in", "/dp/x")


def test_ebay_label_allows_basket(al):
    (rule,) = al.policy.rules_for("click", "ebay.com", "/anything")
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
    al = ActionAllowlist.from_file(f)
    assert al.policy.read_policy.is_allowed("anything.example.com", "/whatever")  # via overrides
    assert al.policy.read_policy.is_allowed("google.com", "/")                    # via Tranco
    assert al.policy.section("click").is_allowed("www.amazon.com", "/dp/X")
    (rule,) = al.policy.rules_for("click", "amazon.com", "/dp/X")
    assert rule.label.search("Add to Cart") and not rule.label.search("Buy Now")


def test_from_file_all_comments_is_deny_all(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("# everything commented out\n# read:\n#   enabled: true\n")
    al = ActionAllowlist.from_file(f)
    assert not al.policy.read_policy.is_allowed("example.com", "/")
    assert not al.policy.section("click").is_allowed("amazon.com", "/")


# -- host canonicalization consistency (H3) ----------------------------------

def test_denylist_not_bypassed_by_trailing_dot():
    # evil.com. resolves to evil.com in the browser, so the denylist must treat
    # them alike (canonical_host strips the trailing dot) or the deny is bypassed.
    al = ActionAllowlist({
        "read": {"tranco": {"enabled": True, "top_n": 1000}},
        "denylist": {"google.com": [".*"]},
    })
    assert al.policy.is_denied("google.com", "/")
    assert al.policy.is_denied("google.com.", "/")                    # trailing-dot form
    assert not al.policy.read_policy.is_allowed("google.com.", "/")   # net: still blocked


def test_denylist_keys_and_lookups_agree_on_www():
    # A rule written with or without www. matches a host written either way —
    # keys and lookups canonicalize identically, so no silently-inert entry.
    for entry in ("tracker.com", "www.tracker.com"):
        al = ActionAllowlist({"denylist": {entry: [".*"]}})
        assert al.policy.is_denied("tracker.com", "/")
        assert al.policy.is_denied("www.tracker.com", "/")


def test_override_matches_trailing_dot_and_www():
    al = ActionAllowlist({"read": {"website_overrides": {"example.com": [".*"]}}})
    assert al.policy.read_policy.is_allowed("example.com", "/")
    assert al.policy.read_policy.is_allowed("example.com.", "/")
    assert al.policy.read_policy.is_allowed("www.example.com", "/")


# -- page-scoped rules (write) ------------------------------------------------

def test_write_label_is_scoped_per_page():
    # amazon.com authorizes different controls on different pages: "add to cart"
    # on a product page, "place your order" only in the checkout pipeline.
    al = ActionAllowlist({"click": {"amazon.com": [
        {"path": ["^/(dp|gp/product)/.*"], "label": "(?i)add to cart"},
        {"path": ["^/gp/buy/.*"], "label": "(?i)place your order"},
    ]}})

    def labels(path):
        return {r.label.pattern for r in al.policy.rules_for("click", "amazon.com", path)}

    assert labels("/dp/B0X") == {"(?i)add to cart"}
    assert labels("/gp/buy/spc") == {"(?i)place your order"}
    # A page matched by no rule authorizes nothing — the whole point.
    assert labels("/gp/css/account/close") == set()


def test_field_ids_are_page_scoped():
    # A label-less field id is typable only on the page whose rule lists it.
    al = ActionAllowlist({"write-text": {"amazon.com": [
        {"path": ["^/gp/css/order-history.*"], "label": "(?i)tip",
         "field_ids": ["tip-input"]},
    ]}})
    (rule,) = al.policy.rules_for("write-text", "amazon.com", "/gp/css/order-history")
    assert rule.field_ids == frozenset({"tip-input"})
    assert al.policy.rules_for("write-text", "amazon.com", "/dp/B0X") == []  # no id here


def test_match_on_url_scopes_a_hash_router_spa():
    # Every SPA page shares path "/"; only match_on: url can tell them apart.
    al = ActionAllowlist({"click": {"secure.splitwise.com": [
        {"path": [r"^/#/friends/\d+$"], "match_on": "url", "label": "(?i)save"},
    ]}})
    friend = al.policy.rules_for("click", "secure.splitwise.com", "/", fragment="/friends/42")
    assert len(friend) == 1
    # The group page (same path "/") is NOT authorized.
    assert al.policy.rules_for("click", "secure.splitwise.com", "/", fragment="/groups/7") == []
    # A path-only match (fragment dropped) would wrongly see them as identical.
    assert al.policy.rules_for("click", "secure.splitwise.com", "/") == []


def test_match_on_url_read_override_scopes_spa_pages():
    al = ActionAllowlist({"read": {"website_overrides": {"app.example.com": [
        {"path": [r"^/#/reports/\d+$"], "match_on": "url"},
    ]}}})
    assert al.policy.read_policy.is_allowed("app.example.com", "/", fragment="/reports/9")
    assert not al.policy.read_policy.is_allowed("app.example.com", "/", fragment="/admin")
    # Listing the host demotes it from any Tranco grant to just these pages.
    assert not al.policy.read_policy.is_allowed("app.example.com", "/other")


def test_page_rule_requires_label_for_write_actions():
    with pytest.raises(ValueError, match="requires a 'label'"):
        ActionAllowlist({"click": {"amazon.com": [{"path": ["^/dp/.*"]}]}})


def test_page_rule_rejects_label_on_read_override():
    with pytest.raises(ValueError, match="only meaningful for a write action"):
        ActionAllowlist({"read": {"website_overrides": {
            "x.com": [{"path": [".*"], "label": ".*"}]}}})


def test_page_rule_rejects_unknown_match_on():
    with pytest.raises(ValueError, match="match_on must be"):
        ActionAllowlist({"click": {"amazon.com": [
            {"path": [".*"], "match_on": "host", "label": ".*"}]}})


def test_legacy_and_page_rule_forms_coexist():
    # Legacy host-wide dict and the new page-rule list load side by side.
    al = ActionAllowlist({"click": {
        "ebay.com": {"paths": [".*"], "label": "(?i)add"},           # legacy
        "amazon.com": [{"path": ["^/dp/.*"], "label": "(?i)add"}],   # page rules
    }})
    assert al.policy.rules_for("click", "ebay.com", "/anything")
    assert al.policy.rules_for("click", "amazon.com", "/dp/x")
    assert al.policy.rules_for("click", "amazon.com", "/other") == []


# -- infra knobs -------------------------------------------------------------

def test_infra_defaults_when_the_section_is_absent():
    al = ActionAllowlist({})
    assert al.max_browser_sessions == DEFAULT_MAX_BROWSER_SESSIONS
    assert al.max_tabs_per_session == DEFAULT_MAX_TABS_PER_SESSION
    assert al.reap_interval_seconds == DEFAULT_REAP_INTERVAL_SECONDS == 7200


def test_infra_reap_interval_is_read_from_the_config():
    al = ActionAllowlist({"infra": {"reap_interval_seconds": 600}})
    assert al.reap_interval_seconds == 600
    # An unset sibling keeps its default rather than following the one that was set.
    assert al.max_tabs_per_session == DEFAULT_MAX_TABS_PER_SESSION


# -- the policy set / container split -----------------------------------------

def test_policy_set_decides_without_the_infra_container():
    # PolicySet is the surface the gates use: it needs only the rule sections,
    # never the process-wide caps, so it can be built (and tested) on its own.
    ps = PolicySet({
        "denylist": {"blocked.test": [".*"]},
        "read": {"website_overrides": {"example.com": ["^/docs/.*"]}},
        "click": {"shop.test": {"paths": [".*"], "label": "(?i)add"}},
    })
    assert ps.read_policy.is_allowed("example.com", "/docs/x")
    assert not ps.read_policy.is_allowed("example.com", "/secret")
    assert ps.is_denied("blocked.test", "/")
    assert ps.rules_for("click", "shop.test", "/cart")
    assert ps.section("click").is_allowed("shop.test", "/cart")


def test_infra_is_not_a_write_action():
    # `infra` is the container's, so the policy set must not mistake it for a
    # write action named "infra" (every unrecognized key is one).
    ps = PolicySet({"infra": {"max_tabs_per_session": 3}})
    assert ps.rules_for("infra", "example.com", "/") == []
    assert not ps.section("infra").is_allowed("example.com", "/")


def test_the_policy_set_is_the_only_route_to_a_decision():
    # The container holds the process-wide caps and the rules apart: a gate is
    # handed `.policy` (the rule set for the request it decides), never the
    # container, so there is no accessor that silently decides against the
    # wrong scope.
    al = ActionAllowlist({"infra": {"max_tabs_per_session": 3},
                          "read": {"website_overrides": {"example.com": [".*"]}}})
    assert isinstance(al.policy, PolicySet)
    assert al.max_tabs_per_session == 3
    assert al.policy.read_policy.is_allowed("example.com", "/")
    for gone in ("read_policy", "denylist", "is_denied", "section", "rules_for"):
        assert not hasattr(al, gone), f"{gone} must be reached through .policy"


# -- profile-scoped rule sets (#139) ------------------------------------------

def _profiles(**bodies) -> ActionAllowlist:
    """An allowlist whose profile keys are absolute paths, as a config's must be."""
    return ActionAllowlist({"profiles": {f"/profiles/{name}": body
                                         for name, body in bodies.items()}})


def test_a_write_rule_under_one_profile_authorizes_only_there():
    al = _profiles(shopper={"click": {"shop.test": {"paths": [".*"], "label": "(?i)add"}}},
                   reader={})
    assert al.policy_for("/profiles/shopper").rules_for("click", "shop.test", "/cart")
    assert al.policy_for("/profiles/reader").rules_for("click", "shop.test", "/cart") == []
    # And in a profile the config never mentions at all.
    assert al.policy_for("/profiles/unknown").rules_for("click", "shop.test", "/cart") == []


def test_a_read_override_under_one_profile_admits_only_there():
    al = _profiles(research={"read": {"website_overrides": {"unranked.test": [".*"]}}})
    assert al.policy_for("/profiles/research").read_policy.is_allowed("unranked.test", "/x")
    assert not al.policy_for("/profiles/other").read_policy.is_allowed("unranked.test", "/x")


def test_a_profile_with_no_entry_gets_exactly_the_global_rules():
    al = ActionAllowlist({
        "read": {"website_overrides": {"example.com": [".*"]}},
        "profiles": {"/profiles/research": {"read": {"website_overrides": {"other.test": [".*"]}}}},
    })
    assert al.policy_for("/profiles/anything-else") is al.policy
    assert al.policy_for("/profiles/anything-else").read_policy.is_allowed("example.com", "/")


def test_no_profiles_block_leaves_every_profile_on_the_global_set():
    # The backward-compatibility guarantee: a config written before `profiles:`
    # existed decides every request with the one global rule set.
    al = ActionAllowlist({"read": {"website_overrides": {"example.com": [".*"]}}})
    assert al.policy_for("/anything") is al.policy
    assert al.profile_dirs() == []


def test_profile_rules_are_additive_over_the_global_ones():
    al = ActionAllowlist({
        "read": {"website_overrides": {"global.test": [".*"]}},
        "click": {"global-shop.test": {"paths": [".*"], "label": "(?i)add"}},
        "profiles": {"/profiles/shopper": {
            "read": {"website_overrides": {"local.test": [".*"]}},
            "click": {"local-shop.test": {"paths": [".*"], "label": "(?i)buy"}},
        }},
    })
    scoped = al.policy_for("/profiles/shopper")
    # Its own rules, AND everything the global set granted.
    assert scoped.read_policy.is_allowed("local.test", "/")
    assert scoped.read_policy.is_allowed("global.test", "/")
    assert scoped.rules_for("click", "local-shop.test", "/")
    assert scoped.rules_for("click", "global-shop.test", "/")
    # The global set is untouched by what the profile added.
    assert not al.policy.read_policy.is_allowed("local.test", "/")
    assert al.policy.rules_for("click", "local-shop.test", "/") == []


def test_rules_for_one_host_union_across_global_and_profile():
    # The same host listed in both places keeps both rule sets — including when
    # the two spell the host differently (www. is stripped by canonicalization,
    # so these must not look like two hosts and lose one).
    al = ActionAllowlist({
        "click": {"shop.test": [{"path": ["^/cart.*"], "label": "(?i)add"}]},
        "profiles": {"/profiles/shopper": {
            "click": {"www.shop.test": [{"path": ["^/checkout.*"], "label": "(?i)pay"}]}}},
    })
    scoped = al.policy_for("/profiles/shopper")
    assert scoped.rules_for("click", "shop.test", "/cart")       # from the global set
    assert scoped.rules_for("click", "shop.test", "/checkout")   # from the profile


def test_denylist_unions_and_still_wins_inside_a_profile():
    al = ActionAllowlist({
        "denylist": {"blocked.test": [".*"]},
        "read": {"website_overrides": {"*": [".*"]}},
        "profiles": {"/profiles/research": {
            "denylist": {"also-blocked.test": [".*"]},
            "read": {"website_overrides": {"blocked.test": [".*"]}},
        }},
    })
    scoped = al.policy_for("/profiles/research")
    # The global denial holds in the profile even though the profile allow-lists it.
    assert scoped.is_denied("blocked.test", "/")
    assert not scoped.read_policy.is_allowed("blocked.test", "/")
    # The profile's own denial holds there...
    assert scoped.is_denied("also-blocked.test", "/")
    # ...and does not leak into any other profile.
    assert not al.policy_for("/profiles/other").is_denied("also-blocked.test", "/")


def test_a_profile_can_turn_the_popularity_net_off_for_itself_only():
    al = ActionAllowlist({
        "read": {"tranco": {"enabled": True}, "website_overrides": {}},
        "profiles": {"/profiles/offline": {"read": {"tranco": {"enabled": False}}}},
    })
    # google.com is rank #1 in the mini fixture: ranked everywhere it is checked.
    assert al.policy.read_policy.is_allowed("google.com", "/")
    # The profile that switched Tranco off has no read grant left at all —
    # turning the net off narrows, it does not open (nothing else allows a read).
    assert not al.policy_for("/profiles/offline").read_policy.is_allowed("google.com", "/")


def test_a_profile_inherits_global_read_settings_it_does_not_restate():
    al = ActionAllowlist({
        "read": {"tranco": {"enabled": True, "top_n": 1000}},
        "profiles": {"/profiles/p": {"read": {"website_overrides": {"unranked.test": [".*"]}}}},
    })
    scoped = al.policy_for("/profiles/p")
    assert scoped.read_policy.is_allowed("google.com", "/")       # inherited Tranco
    assert scoped.read_policy.is_allowed("unranked.test", "/")    # its own override


def test_profile_keys_are_canonicalized_like_session_paths():
    # Compared as paths, never as strings: the separator is the platform's, so a
    # literal "<home>/.cache/..." would only ever match on POSIX.
    cache = Path.home() / ".cache" / "browden"
    al = ActionAllowlist({"profiles": {
        "~/.cache/browden/scratch": {"read": {"website_overrides": {"ok.test": [".*"]}}}}})
    assert [Path(p) for p in al.profile_dirs()] == [(cache / "scratch").resolve()]
    # Every spelling of that one directory finds it.
    for spelling in ("~/.cache/browden/scratch", str(cache / "scratch"),
                     str(cache / "x" / ".." / "scratch")):
        assert al.policy_for(spelling).read_policy.is_allowed("ok.test", "/"), spelling
    # A different directory does not.
    assert not al.policy_for(str(cache / "other")).read_policy.is_allowed("ok.test", "/")


def test_an_unusable_profile_path_falls_back_to_the_global_set():
    # A gate must never crash on a odd path — and the fallback is the narrow
    # direction, since a profile's rules only ever add to the global ones.
    al = _profiles(p={"read": {"website_overrides": {"ok.test": [".*"]}}})
    assert al.policy_for("some\x00garbage") is al.policy


# -- allow_all (#139) ---------------------------------------------------------
#
# The mini Tranco fixture ranks google.com (#1); "unranked.test" is in no
# snapshot, which is what makes the popularity branch observable here.

def _allow_all(read: dict | None = None, **rest) -> "PolicySet":
    body = {"allow_all": True, **rest}
    if read is not None:
        body["read"] = read
    return ActionAllowlist({"profiles": {"/profiles/scratch": body}}).policy_for("/profiles/scratch")


def test_allow_all_permits_every_write_action_on_any_page():
    scratch = _allow_all()
    for action in ("click", "write-text", "press-key", "some-future-action"):
        rules = scratch.rules_for(action, "anything.test", "/deep/page", "q=1", "frag")
        assert rules, action
        assert any(r.label is not None and r.label.fullmatch("Whatever the control says")
                   for r in rules), action
    # press-key needs the key in the rule's `keys`; allow_all authorizes the
    # control keys the gate accepts (and no character keys — that gate is
    # separate and unmoved).
    assert "Enter" in scratch.rules_for("press-key", "anything.test", "/")[0].keys
    assert "a" not in scratch.rules_for("press-key", "anything.test", "/")[0].keys


def test_allow_all_reads_ranked_hosts_but_still_refuses_unranked_ones():
    # The acceptance criterion: allow_all is "browse the established web", not
    # "browse anything". Tranco is ON here without the profile saying so.
    scratch = _allow_all()
    assert scratch.read_policy.is_allowed("google.com", "/search")
    assert not scratch.read_policy.is_allowed("unranked.test", "/")


def test_allow_all_turns_tranco_on_even_when_the_global_config_had_it_off():
    # The net is turned ON for an allow_all set rather than inherited: opting out
    # is something the profile says in its own block, so a global
    # `tranco: {enabled: false}` doesn't quietly make allow_all mean "anything".
    al = ActionAllowlist({
        "read": {"tranco": {"enabled": False}},
        "profiles": {"/profiles/scratch": {"allow_all": True}},
    })
    scoped = al.policy_for("/profiles/scratch")
    assert scoped.read_policy.is_allowed("google.com", "/")          # ranked -> allowed
    assert not scoped.read_policy.is_allowed("unranked.test", "/")   # the net is on


def test_allow_all_plus_explicit_tranco_off_admits_an_unranked_host():
    scratch = _allow_all(read={"tranco": {"enabled": False}})
    assert scratch.read_policy.is_allowed("unranked.test", "/")
    assert scratch.read_policy.is_allowed("google.com", "/")


def test_allow_all_is_not_narrowed_by_an_inherited_page_scoped_override():
    al = ActionAllowlist({
        "read": {"tranco": {"enabled": False},
                 "website_overrides": {"docs.test": ["^/public/.*"]}},
        "profiles": {"/profiles/scratch": {"allow_all": True,
                                           "read": {"tranco": {"enabled": False}}}},
    })
    # Globally the override demotes docs.test to /public/* — its whole purpose.
    assert al.policy.read_policy.is_allowed("docs.test", "/public/x")
    assert not al.policy.read_policy.is_allowed("docs.test", "/private/x")
    # In the profile that allows everything, that inherited page-scope only adds.
    assert al.policy_for("/profiles/scratch").read_policy.is_allowed("docs.test", "/private/x")


def test_allow_all_with_the_net_on_still_refuses_unranked_pages_outside_an_override():
    # The override adds the pages it names; the rest of an unranked host is left
    # to the popularity check, which is on.
    scratch = _allow_all(read={"website_overrides": {"unranked.test": ["^/ok/.*"]}})
    assert scratch.read_policy.is_allowed("unranked.test", "/ok/x")
    assert not scratch.read_policy.is_allowed("unranked.test", "/elsewhere")


def test_allow_all_does_not_open_file_or_plaintext_http():
    # The scheme gate asks whether the operator named THIS host explicitly.
    # allow_all names nothing, so it can never answer yes.
    scratch = _allow_all(read={"tranco": {"enabled": False}})
    assert not scratch.read_policy.override_has_host("localhost")
    assert not scratch.read_policy.override_has_host("")       # the file:// host
    # Naming the host is still what opts it in.
    named = _allow_all(read={"tranco": {"enabled": False},
                             "website_overrides": {"localhost": [".*"]}})
    assert named.read_policy.override_has_host("localhost")


def test_denylist_still_wins_inside_an_allow_all_profile():
    al = ActionAllowlist({
        "denylist": {"blocked.test": [".*"]},
        "profiles": {"/profiles/scratch": {"allow_all": True,
                                           "denylist": {"local-block.test": [".*"]}}},
    })
    scratch = al.policy_for("/profiles/scratch")
    assert scratch.is_denied("blocked.test", "/")
    assert scratch.is_denied("local-block.test", "/")
    assert not scratch.read_policy.is_allowed("blocked.test", "/")


def test_allow_all_stays_inside_its_own_profile():
    al = ActionAllowlist({"profiles": {
        "/profiles/scratch": {"allow_all": True},
        "/profiles/narrow": {},
    }})
    assert al.policy_for("/profiles/scratch").rules_for("click", "shop.test", "/")
    assert al.policy_for("/profiles/narrow").rules_for("click", "shop.test", "/") == []
    assert al.policy.rules_for("click", "shop.test", "/") == []
    assert not al.policy_for("/profiles/narrow").read_policy.is_allowed("unranked.test", "/")


def test_allow_all_is_not_a_write_action_named_allow_all():
    scratch = _allow_all()
    # The flag must not fall through to the "every other key is a write action"
    # branch — that would try to read `True` as a host -> rule mapping.
    assert scratch.section("click").is_allowed("anything.test", "/")


# The shipped sample (configs/samples/read_only_on_popular_websites.yaml) is
# covered by test/unit/configs/test_loader.py through the schema-validating loader.
