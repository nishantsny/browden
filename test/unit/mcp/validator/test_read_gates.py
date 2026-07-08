import pytest

from browden.mcp.validator import (
    ActionAllowlist,
    Allowlist,
    ValidationError,
    ensure_url_is_in_allowlist,
    is_url_allowed,
    validate_url,
)


@pytest.fixture
def amazon_only() -> Allowlist:
    """Restrictive allowlist matching the original production policy."""
    return Allowlist(
        {
            "amazon.com": [
                "^/$",
                "^/gp/your-account/order-history/?$",
                "^/dp/[A-Z0-9]{10}/?$",
                "^/[^/]+/dp/[A-Z0-9]{10}/?$",
            ]
        }
    )


@pytest.fixture
def wildcard() -> Allowlist:
    return Allowlist({"*": [".*"]})


def test_bare_domain_normalized(amazon_only):
    assert validate_url("amazon.com", amazon_only) == "https://amazon.com"


def test_homepage_allowed_with_trailing_slash(amazon_only):
    assert validate_url("https://amazon.com/", amazon_only) == "https://amazon.com/"


def test_www_host_matches_allowlist(amazon_only):
    assert validate_url("https://www.amazon.com/", amazon_only) == "https://www.amazon.com/"


def test_order_history_allowed(amazon_only):
    url = "https://www.amazon.com/gp/your-account/order-history"
    assert validate_url(url, amazon_only) == url


def test_product_dp_allowed(amazon_only):
    url = "https://www.amazon.com/dp/B09B8V1LZ3"
    assert validate_url(url, amazon_only) == url


def test_product_slug_dp_allowed(amazon_only):
    url = "https://www.amazon.com/Amazon-Echo-Dot-5th-Gen/dp/B09B8V1LZ3"
    assert validate_url(url, amazon_only) == url


def test_query_preserved_for_allowlisted_url(amazon_only):
    url = "https://www.amazon.com/dp/B09B8V1LZ3?th=1&ref=foo"
    assert validate_url(url, amazon_only) == url


def test_fragment_preserved_for_allowlisted_url(amazon_only):
    url = "https://amazon.com/#deals"
    assert validate_url(url, amazon_only) == url


def test_space_preserved_in_query(amazon_only):
    url = "https://amazon.com/?q=hello world"
    assert validate_url(url, amazon_only) == url


def test_non_allowlisted_path_rejected(amazon_only):
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_url("https://amazon.com/orders", amazon_only)


def test_non_allowlisted_host_rejected(amazon_only):
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_url("https://evil.example.com/", amazon_only)


def test_no_host_rejected(amazon_only):
    with pytest.raises(ValidationError, match="no host"):
        validate_url("http://", amazon_only)


def test_empty_string_rejected(amazon_only):
    with pytest.raises(ValidationError):
        validate_url("", amazon_only)


def test_wildcard_host_allows_unknown(wildcard):
    url = "https://anything.example.com/whatever/path"
    assert validate_url(url, wildcard) == url


def test_wildcard_host_only_used_when_specific_host_absent():
    al = Allowlist({"amazon.com": ["^/$"], "*": [".*"]})
    # Specific host has its own (narrow) rules — fallback NOT used.
    with pytest.raises(ValidationError):
        validate_url("https://amazon.com/orders", al)
    # Unknown host falls through to wildcard.
    assert validate_url("https://other.example.com/anything", al) == "https://other.example.com/anything"


# -- is_url_allowed / ensure_url_is_in_allowlist (the read-tool gate, H2) ------

_READ = ActionAllowlist({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"amazon.com": [".*"]}},
})
_DENIED = ActionAllowlist({"denylist": {"amazon.com": [".*"]}})
AMAZON = "https://www.amazon.com/dp/B0FBRRM2VQ"


def test_is_url_allowed_reflects_the_gate():
    assert is_url_allowed(_READ.read_policy, AMAZON)            # amazon override
    assert not is_url_allowed(_READ.read_policy, "https://evil.example.com/x")


def test_is_url_allowed_hostless_follows_the_gate():
    # A hostless URL (about:blank / data:) has host "" and is decided by the gate
    # like any other — a host-specific policy denies it, a '*' override admits it.
    # (Per-scheme handling is scheme-allowlisting, added later.)
    assert not is_url_allowed(_READ.read_policy, "about:blank")
    assert not is_url_allowed(_READ.read_policy, "data:text/html,<h1>hi</h1>")
    wildcard = ActionAllowlist({"read": {"website_overrides": {"*": [".*"]}}})
    assert is_url_allowed(wildcard.read_policy, "about:blank")


def test_ensure_url_is_in_allowlist_passes_and_raises():
    ensure_url_is_in_allowlist(_READ, AMAZON)  # no raise
    with pytest.raises(ValidationError, match="read allowlist"):
        ensure_url_is_in_allowlist(_READ, "https://evil.example.com/x")


def test_ensure_url_is_in_allowlist_respects_denylist():
    assert not is_url_allowed(_DENIED.read_policy, AMAZON)
    with pytest.raises(ValidationError):
        ensure_url_is_in_allowlist(_DENIED, AMAZON)
