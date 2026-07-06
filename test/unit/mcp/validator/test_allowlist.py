import pytest

from browser_guard.mcp.validator import ActionAllowlist


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
    assert al.read_policy.is_allowed("anything.example.com", "/whatever")


def test_read_overrides_can_path_scope():
    al = ActionAllowlist({"read": {"website_overrides": {"example.com": ["^/docs/"]}}})
    assert al.read_policy.is_allowed("example.com", "/docs/intro")
    assert not al.read_policy.is_allowed("example.com", "/secret")
    assert not al.read_policy.is_allowed("other.com", "/docs/intro")


def test_read_default_denies_when_nothing_listed():
    # No read block and no overrides => fail closed.
    assert not ActionAllowlist({}).read_policy.is_allowed("example.com", "/")


# -- read policy: Tranco ------------------------------------------------------

def test_read_tranco_allows_top_sites_and_subdomains():
    al = ActionAllowlist({"read": {"tranco": {"enabled": True, "top_n": 1000}}})
    # google.com is Tranco rank #1 — in any top_n.
    assert al.read_policy.is_allowed("google.com", "/")
    assert al.read_policy.is_allowed("mail.google.com", "/inbox")  # subdomain covered
    assert al.read_policy.is_allowed("www.google.com", "/")        # www stripped
    # A lookalike must not ride on the listed domain.
    assert not al.read_policy.is_allowed("google.com.evil.co", "/")
    # A domain that is nowhere near the top is denied.
    assert not al.read_policy.is_allowed("nonexistent-xyz-987654.test", "/")


def test_read_tranco_top_n_is_a_cutoff():
    # top_n=1 keeps only rank #1 (google.com); rank #2 (cloudflare.com) is out.
    al = ActionAllowlist({"read": {"tranco": {"enabled": True, "top_n": 1}}})
    assert al.read_policy.is_allowed("google.com", "/")
    assert not al.read_policy.is_allowed("cloudflare.com", "/")


def test_read_tranco_disabled_by_default():
    # tranco present but enabled:false => contributes no allows.
    al = ActionAllowlist({"read": {"tranco": {"enabled": False}}})
    assert not al.read_policy.is_allowed("google.com", "/")


def test_read_tranco_and_overrides_are_both_honored():
    al = ActionAllowlist({"read": {
        "tranco": {"enabled": True, "top_n": 100},
        "website_overrides": {"intranet.corp": [".*"]},
    }})
    assert al.read_policy.is_allowed("google.com", "/")        # via Tranco
    assert al.read_policy.is_allowed("intranet.corp", "/wiki")  # via overrides


def test_override_triumphs_over_tranco_and_can_restrict():
    # google.com is in Tranco (blanket allow), but an explicit override governs
    # it — so it is path-scoped, and Tranco no longer waves the rest through.
    al = ActionAllowlist({"read": {
        "tranco": {"enabled": True, "top_n": 1000},
        "website_overrides": {"google.com": ["^/allowed/"]},
    }})
    assert al.read_policy.is_allowed("google.com", "/allowed/x")
    assert not al.read_policy.is_allowed("google.com", "/other")   # override restricts
    # A Tranco host WITHOUT an override still rides on Tranco.
    assert al.read_policy.is_allowed("cloudflare.com", "/anything")


def test_wildcard_override_scopes_every_host_over_tranco():
    al = ActionAllowlist({"read": {
        "tranco": {"enabled": True, "top_n": 1000},
        "website_overrides": {"*": ["^/docs/"]},
    }})
    assert al.read_policy.is_allowed("google.com", "/docs/x")  # top site, path matches
    assert not al.read_policy.is_allowed("google.com", "/home")  # scoped even for a top site


def test_denylist_still_beats_a_triumphing_override():
    al = ActionAllowlist({
        "read": {"website_overrides": {"*": [".*"]}},
        "denylist": {"evil.test": [".*"]},
    })
    assert al.read_policy.is_allowed("anything.test", "/")   # overrides open the web
    assert not al.read_policy.is_allowed("evil.test", "/")   # denylist still wins


# -- read policy: master switch ----------------------------------------------

def test_read_disabled_allows_everything_non_denied():
    al = ActionAllowlist({"read": {"enabled": False}})
    assert al.read_policy.is_allowed("literally-anything.test", "/x")


# -- denylist -----------------------------------------------------------------

def test_denylist_vetoes_even_tranco():
    al = ActionAllowlist({
        "read": {"tranco": {"enabled": True, "top_n": 1000}},
        "denylist": {"google.com": [".*"]},
    })
    assert not al.read_policy.is_allowed("google.com", "/")   # denied despite Tranco
    assert al.read_policy.is_allowed("cloudflare.com", "/")   # other top sites unaffected


def test_denylist_vetoes_even_when_read_disabled():
    al = ActionAllowlist({
        "read": {"enabled": False},
        "denylist": {"evil.test": [".*"]},
    })
    assert not al.read_policy.is_allowed("evil.test", "/")
    assert al.read_policy.is_allowed("anything-else.test", "/")


def test_denylist_can_path_scope_and_is_queryable():
    al = ActionAllowlist({"denylist": {"example.com": ["^/checkout"]}})
    assert al.is_denied("example.com", "/checkout/pay")
    assert not al.is_denied("example.com", "/browse")
    assert not al.is_denied("other.com", "/checkout")


def test_empty_denylist_denies_nothing():
    assert not ActionAllowlist({"denylist": {}}).is_denied("anywhere.test", "/x")


# -- write (click) section ----------------------------------------------------

def test_click_allows_listed_hosts(al):
    assert al.section("click").is_allowed("amazon.com", "/dp/B0FBRRM2VQ")
    assert al.section("click").is_allowed("www.amazon.com", "/")  # www stripped


def test_click_denies_unlisted_host(al):
    assert not al.section("click").is_allowed("evil.example.com", "/")
    # No wildcard fallback in the write section.
    assert not al.section("click").is_allowed("amazon.de", "/")


def test_click_respects_per_host_paths(al):
    assert not al.section("click").is_allowed("amazon.in", "/gp/cart")  # only /dp/*
    assert al.section("click").is_allowed("amazon.in", "/dp/B0FBRRM2VQ")


def test_unknown_action_default_denies(al):
    assert not al.section("delete_account").is_allowed("amazon.com", "/")


def test_label_pattern_lookup(al):
    pat = al.label_pattern("click", "amazon.com")
    assert pat is not None and pat.search("Add to Cart")
    assert al.label_pattern("click", "www.amazon.com") is not None  # canonicalized
    assert al.label_pattern("click", "evil.example.com") is None
    assert al.label_pattern("read", "amazon.com") is None  # read is not a write section


def test_ebay_label_allows_basket(al):
    pat = al.label_pattern("click", "ebay.com")
    assert pat.search("Add to basket") and pat.search("Add to cart")


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
    assert al.read_policy.is_allowed("anything.example.com", "/whatever")  # via overrides
    assert al.read_policy.is_allowed("google.com", "/")                    # via Tranco
    assert al.section("click").is_allowed("www.amazon.com", "/dp/X")
    pat = al.label_pattern("click", "amazon.com")
    assert pat is not None and pat.search("Add to Cart") and not pat.search("Buy Now")


def test_from_file_all_comments_is_deny_all(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("# everything commented out\n# read:\n#   enabled: true\n")
    al = ActionAllowlist.from_file(f)
    assert not al.read_policy.is_allowed("example.com", "/")
    assert not al.section("click").is_allowed("amazon.com", "/")


# The shipped sample (now at configs/samples/allowlist.yaml) is covered by
# test/unit/configs/test_loader.py through the schema-validating loader.
