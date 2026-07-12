import pytest

from browden.mcp.validator import (
    ActionAllowlist,
    Allowlist,
    ValidationError,
    ensure_url_allowed,
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
        },
        full_match=True,
    )


@pytest.fixture
def wildcard() -> Allowlist:
    return Allowlist({"*": [".*"]}, full_match=True)


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
    al = Allowlist({"amazon.com": ["^/$"], "*": [".*"]}, full_match=True)
    # Specific host has its own (narrow) rules — fallback NOT used.
    with pytest.raises(ValidationError):
        validate_url("https://amazon.com/orders", al)
    # Unknown host falls through to wildcard.
    assert validate_url("https://other.example.com/anything", al) == "https://other.example.com/anything"


def test_validate_url_passes_browser_internal_tabs_through(amazon_only):
    # about:blank and the browser new-tab page are special-cased before any gating —
    # returned as-is even under a restrictive allowlist, never coerced to https://.
    assert validate_url("about:blank", amazon_only) == "about:blank"
    assert validate_url("chrome://new-tab-page/", amazon_only) == "chrome://new-tab-page/"


# -- ensure_url_allowed (the read-tool gate, H2) ------------------------------

_READ = ActionAllowlist({
    "read": {"enabled": True, "tranco": {"enabled": False},
             "website_overrides": {"amazon.com": [".*"]}},
})
_DENIED = ActionAllowlist({"denylist": {"amazon.com": [".*"]}})
AMAZON = "https://www.amazon.com/dp/B0FBRRM2VQ"


def test_ensure_url_allowed_true_for_allowed_host():
    assert ensure_url_allowed(_READ, AMAZON) is True


def test_ensure_url_allowed_false_for_denied_host():
    assert ensure_url_allowed(_READ, "https://evil.example.com/x") is False


def test_ensure_url_allowed_false_under_denylist():
    assert ensure_url_allowed(_DENIED, AMAZON) is False


def test_ensure_url_allowed_true_for_browser_internal_tabs():
    # A not-yet-navigated tab is readable even under a host-specific policy or a
    # denylist — validate_url special-cases about:blank / the new-tab page.
    for u in ("about:blank", "chrome://new-tab-page/"):
        assert ensure_url_allowed(_READ, u) is True
        assert ensure_url_allowed(_DENIED, u) is True
