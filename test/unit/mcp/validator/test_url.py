import pytest

from browser_guard.mcp.validator import ValidationError, validate_url


def test_bare_domain_normalized():
    assert validate_url("amazon.com") == "https://amazon.com"


def test_homepage_allowed_with_trailing_slash():
    assert validate_url("https://amazon.com/") == "https://amazon.com/"


def test_www_host_matches_allowlist():
    assert validate_url("https://www.amazon.com/") == "https://www.amazon.com/"


def test_order_history_allowed():
    url = "https://www.amazon.com/gp/your-account/order-history"
    assert validate_url(url) == url


def test_product_dp_allowed():
    url = "https://www.amazon.com/dp/B09B8V1LZ3"
    assert validate_url(url) == url


def test_product_slug_dp_allowed():
    url = "https://www.amazon.com/Amazon-Echo-Dot-5th-Gen/dp/B09B8V1LZ3"
    assert validate_url(url) == url


def test_query_preserved_for_allowlisted_url():
    url = "https://www.amazon.com/dp/B09B8V1LZ3?th=1&ref=foo"
    assert validate_url(url) == url


def test_fragment_preserved_for_allowlisted_url():
    url = "https://amazon.com/#deals"
    assert validate_url(url) == url


def test_space_preserved_in_query():
    url = "https://amazon.com/?q=hello world"
    assert validate_url(url) == url


def test_non_allowlisted_path_rejected():
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_url("https://amazon.com/orders")


def test_non_allowlisted_host_rejected():
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_url("https://evil.example.com/")


def test_no_host_rejected():
    with pytest.raises(ValidationError, match="no host"):
        validate_url("http://")


def test_empty_string_rejected():
    with pytest.raises(ValidationError):
        validate_url("")
