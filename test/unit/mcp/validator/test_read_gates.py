import pytest

from browden.mcp.validator import (
    ActionAllowlist,
    Allowlist,
    ReadPolicy,
    ValidationError,
    ensure_url_allowed,
    validate_frame_entry,
    validate_url,
)


@pytest.fixture
def amazon_only() -> Allowlist:
    """Restrictive allowlist matching the original production policy."""
    return Allowlist.create_allowlist(
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
    return Allowlist.create_allowlist({"*": [".*"]})


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
    al = Allowlist.create_allowlist({"amazon.com": ["^/$"], "*": [".*"]})
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


# -- M2: scheme allowlisting on the read policy -------------------------------

def _read_policy(overrides: dict) -> ReadPolicy:
    """A ReadPolicy whose website_overrides are ``overrides`` (Tranco off)."""
    return ActionAllowlist({"read": {"website_overrides": overrides}}).read_policy


def test_https_always_allowed_on_read_policy():
    rp = _read_policy({"*": [".*"]})
    assert validate_url("https://anything.example.com/x", rp) == "https://anything.example.com/x"


def test_non_https_rejected_by_default_even_with_open_web():
    # website_overrides "*": [".*"] opens the whole web for reads, but only over
    # https — file:// / ftp:// / plaintext http on a non-named host stay blocked.
    rp = _read_policy({"*": [".*"]})
    for url in ("file:///etc/passwd", "ftp://ftp.example.com/x", "http://example.com/"):
        with pytest.raises(ValidationError, match="scheme not allowed"):
            validate_url(url, rp)


def test_http_re_enabled_by_explicit_localhost_override():
    rp = _read_policy({"localhost": [".*"]})
    url = "http://localhost:8000/status"
    assert validate_url(url, rp) == url


def test_file_re_enabled_by_explicit_empty_host_override():
    # file:///… carries an empty host; scoping it needs an explicit "" entry.
    rp = _read_policy({"": ["^/home/me/.*"]})
    assert validate_url("file:///home/me/notes.txt", rp) == "file:///home/me/notes.txt"
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_url("file:///etc/passwd", rp)  # scheme ok, path not allowed


def test_wildcard_override_does_not_re_enable_non_https():
    # "*" opens hosts for https but is NOT an explicit per-host opt-in, so it must
    # not silently re-enable file:// or plaintext http for arbitrary hosts.
    rp = _read_policy({"*": [".*"]})
    with pytest.raises(ValidationError, match="scheme not allowed"):
        validate_url("http://localhost:8000/x", rp)
    with pytest.raises(ValidationError, match="scheme not allowed"):
        validate_url("file:///home/me/notes.txt", rp)


# -- validate_frame_entry (v1 same-origin iframe gate) ------------------------

def test_frame_entry_allows_same_origin_read_allowed():
    rp = _read_policy({"*": [".*"]})
    assert validate_frame_entry("https://app.example.com/page",
                                "https://app.example.com/api/widget", rp) is None


def test_frame_entry_treats_www_as_same_origin():
    # canonical_host drops a leading www., so www.example.com and example.com are
    # the same origin for the gate.
    rp = _read_policy({"*": [".*"]})
    assert validate_frame_entry("https://example.com/p",
                                "https://www.example.com/inner", rp) is None


def test_frame_entry_refuses_cross_origin_even_when_read_allowed():
    rp = _read_policy({"*": [".*"]})  # the whole (https) web is readable...
    with pytest.raises(ValidationError, match="cross-origin"):
        # ...but a different-host frame is still refused in v1.
        validate_frame_entry("https://app.example.com/p",
                             "https://ads.other.com/frame", rp)


def test_frame_entry_refuses_frame_document_not_read_allowed():
    # Same host (same-origin) but the frame's path is off the read allowlist: the
    # read gate fails before same-origin can admit it.
    rp = _read_policy({"app.example.com": ["^/ok"]})
    with pytest.raises(ValidationError, match="not on allowlist"):
        validate_frame_entry("https://app.example.com/ok",
                             "https://app.example.com/blocked", rp)


def test_frame_entry_refuses_non_https_frame_document():
    rp = _read_policy({"*": [".*"]})
    with pytest.raises(ValidationError, match="scheme not allowed"):
        validate_frame_entry("https://app.example.com/p",
                             "http://app.example.com/inner", rp)
