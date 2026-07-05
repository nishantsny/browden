import pytest

from browser_guard.mcp.validator import ActionAllowlist


@pytest.fixture
def al() -> ActionAllowlist:
    return ActionAllowlist(
        {
            "read": {"*": [".*"]},
            "add_to_cart": {
                "amazon.com": {"paths": [".*"], "label": "(?i)\\badd to cart\\b"},
                "amazon.in": {"paths": ["^/dp/.*"], "label": "(?i)\\badd to cart\\b"},
                "ebay.com": {"paths": [".*"], "label": "(?i)\\badd to (cart|basket)\\b"},
            },
        }
    )


def test_read_section_is_wide_open(al):
    assert al.section("read").is_allowed("anything.example.com", "/whatever")


def test_add_to_cart_allows_listed_hosts(al):
    assert al.section("add_to_cart").is_allowed("amazon.com", "/dp/B0FBRRM2VQ")
    assert al.section("add_to_cart").is_allowed("www.amazon.com", "/")  # www stripped


def test_add_to_cart_denies_unlisted_host(al):
    assert not al.section("add_to_cart").is_allowed("evil.example.com", "/")
    # No wildcard fallback in the write section.
    assert not al.section("add_to_cart").is_allowed("amazon.de", "/")


def test_add_to_cart_respects_per_host_paths(al):
    assert not al.section("add_to_cart").is_allowed("amazon.in", "/gp/cart")  # only /dp/*
    assert al.section("add_to_cart").is_allowed("amazon.in", "/dp/B0FBRRM2VQ")


def test_unknown_action_default_denies(al):
    assert not al.section("delete_account").is_allowed("amazon.com", "/")


def test_label_pattern_lookup(al):
    pat = al.label_pattern("add_to_cart", "amazon.com")
    assert pat is not None and pat.search("Add to Cart")
    assert al.label_pattern("add_to_cart", "www.amazon.com") is not None  # canonicalized
    assert al.label_pattern("add_to_cart", "evil.example.com") is None
    assert al.label_pattern("read", "amazon.com") is None  # list-form section has no labels


def test_ebay_label_allows_basket(al):
    pat = al.label_pattern("add_to_cart", "ebay.com")
    assert pat.search("Add to basket") and pat.search("Add to cart")


def test_list_and_object_forms_coexist(al):
    # read uses list form, add_to_cart uses object form — both parse.
    assert al.section("read").is_allowed("x.com", "/")
    assert al.section("add_to_cart").is_allowed("amazon.com", "/")


# -- YAML loading -------------------------------------------------------------

def test_from_file_parses_yaml(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        '  "*": [".*"]\n'
        "add_to_cart:\n"
        "  amazon.com:\n"
        '    paths: [".*"]\n'
        "    label: '(?i)\\badd to cart\\b'\n"
    )
    al = ActionAllowlist.from_file(f)
    assert al.section("read").is_allowed("anything.example.com", "/whatever")
    assert al.section("add_to_cart").is_allowed("www.amazon.com", "/dp/X")
    pat = al.label_pattern("add_to_cart", "amazon.com")
    assert pat is not None and pat.search("Add to Cart") and not pat.search("Buy Now")


def test_from_file_all_comments_is_deny_all(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("# everything commented out\n# read:\n#   '*': ['.*']\n")
    al = ActionAllowlist.from_file(f)
    assert not al.section("read").is_allowed("example.com", "/")
    assert not al.section("add_to_cart").is_allowed("amazon.com", "/")


def test_shipped_default_reads_open_writes_denied():
    # The allowlist.yaml that ships with the package: read is wide open, the
    # add_to_cart block is present only as a commented-out showcase.
    from pathlib import Path
    import browser_guard.mcp.validator as validator
    shipped = Path(validator.__file__).parent / "allowlist.yaml"
    al = ActionAllowlist.from_file(shipped)
    assert al.section("read").is_allowed("anything.example.com", "/whatever")
    assert not al.section("add_to_cart").is_allowed("amazon.com", "/dp/X")
    assert al.label_pattern("add_to_cart", "amazon.com") is None
