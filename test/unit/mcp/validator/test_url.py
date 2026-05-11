import pytest

from browser_guard.mcp.validator import ValidationError, validate_url


def test_bare_domain_normalized():
    assert validate_url("amazon.com") == "https://amazon.com"


def test_path_allowed():
    assert validate_url("https://amazon.com/orders") == "https://amazon.com/orders"


def test_query_rejected():
    with pytest.raises(ValidationError, match="Query parameters"):
        validate_url("https://amazon.com/orders?ref=foo")


def test_fragment_rejected():
    with pytest.raises(ValidationError, match="fragments"):
        validate_url("https://amazon.com/page#section")


def test_no_host_rejected():
    with pytest.raises(ValidationError, match="no host"):
        validate_url("http://")


def test_empty_string_rejected():
    with pytest.raises(ValidationError):
        validate_url("")
