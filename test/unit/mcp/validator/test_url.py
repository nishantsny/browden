import pytest

from browden.mcp.validator import Allowlist, ValidationError, validate_url


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
        },
        full_match=True,
    )


@pytest.fixture
def wildcard() -> Allowlist:
    return Allowlist({"*": [".*"]}, full_match=True)


def test_bare_domain_normalized(amazon_only):
    assert validate_url("amazon.com", allowlist=amazon_only) == "https://amazon.com"


def test_homepage_allowed_with_trailing_slash(amazon_only):
    assert validate_url("https://amazon.com/", allowlist=amazon_only) == "https://amazon.com/"


def test_www_host_matches_allowlist(amazon_only):
    assert validate_url("https://www.amazon.com/", allowlist=amazon_only) == "https://www.amazon.com/"


def test_order_history_allowed(amazon_only):
    url = "https://www.amazon.com/gp/your-account/order-history"
    assert validate_url(url, allowlist=amazon_only) == url


def test_product_dp_allowed(amazon_only):
    url = "https://www.amazon.com/dp/B09B8V1LZ3"
    assert validate_url(url, allowlist=amazon_only) == url


def test_product_slug_dp_allowed(amazon_only):
    url = "https://www.amazon.com/Amazon-Echo-Dot-5th-Gen/dp/B09B8V1LZ3"
    assert validate_url(url, allowlist=amazon_only) == url


def test_query_preserved_for_allowlisted_url(amazon_only):
    url = "https://www.amazon.com/dp/B09B8V1LZ3?th=1&ref=foo"
    assert validate_url(url, allowlist=amazon_only) == url


def test_fragment_preserved_for_allowlisted_url(amazon_only):
    url = "https://amazon.com/#deals"
    assert validate_url(url, allowlist=amazon_only) == url


def test_space_preserved_in_query(amazon_only):
    url = "https://amazon.com/?q=hello world"
    assert validate_url(url, allowlist=amazon_only) == url


def test_non_allowlisted_path_rejected(amazon_only):
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_url("https://amazon.com/orders", allowlist=amazon_only)


def test_non_allowlisted_host_rejected(amazon_only):
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_url("https://evil.example.com/", allowlist=amazon_only)


def test_no_host_rejected(amazon_only):
    with pytest.raises(ValidationError, match="no host"):
        validate_url("http://", allowlist=amazon_only)


def test_empty_string_rejected(amazon_only):
    with pytest.raises(ValidationError):
        validate_url("", allowlist=amazon_only)


def test_about_blank_allowed_explicitly(amazon_only):
    # about:blank is the inert empty page (fresh tab / redirect-guard reset target);
    # it's allowed directly, without a host rule, on any gate.
    assert validate_url("about:blank", allowlist=amazon_only) == "about:blank"


def test_wildcard_host_allows_unknown(wildcard):
    url = "https://anything.example.com/whatever/path"
    assert validate_url(url, allowlist=wildcard) == url


def test_wildcard_host_only_used_when_specific_host_absent():
    al = Allowlist({"amazon.com": ["^/$"], "*": [".*"]}, full_match=True)
    # Specific host has its own (narrow) rules — fallback NOT used.
    with pytest.raises(ValidationError):
        validate_url("https://amazon.com/orders", allowlist=al)
    # Unknown host falls through to wildcard.
    assert validate_url("https://other.example.com/anything", allowlist=al) == "https://other.example.com/anything"
