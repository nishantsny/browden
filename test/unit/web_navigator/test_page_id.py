"""Unit tests for the page-id wire-format helpers (format/split pair)."""
import pytest

from browser_guard.web_navigator.page_id import (
    SEPARATOR,
    format_page_id,
    split_page_id,
)


def test_format_composes_namespace_and_handle():
    assert format_page_id("ab12cd34", "CDwindow-XYZ") == "ab12cd34-CDwindow-XYZ"


def test_split_returns_namespace_and_handle():
    assert split_page_id("ab12cd34-CDwindow-XYZ") == ("ab12cd34", "CDwindow-XYZ")


def test_split_boundary_is_the_first_separator():
    # The namespace never contains the separator, so only the first dash is the
    # boundary — a Selenium handle keeps its own dashes intact.
    assert split_page_id("ns-a-b-c") == ("ns", "a-b-c")


def test_unnamespaced_id_yields_empty_namespace():
    # No separator at all -> the whole string is the handle, namespace empty.
    assert split_page_id("barehandle") == ("", "barehandle")


@pytest.mark.parametrize("namespace,handle", [
    ("ab12cd34", "CDwindow-11112222"),   # realistic digest + Selenium handle
    ("f0", "h"),                          # minimal
    ("deadbeef", "CDwindow-A-B-C-D"),     # handle with several separators
    ("aabbccdd", ""),                     # empty handle
])
def test_split_is_inverse_of_format(namespace, handle):
    assert split_page_id(format_page_id(namespace, handle)) == (namespace, handle)


def test_format_uses_the_declared_separator():
    out = format_page_id("ns", "h")
    assert SEPARATOR in out
    assert out == f"ns{SEPARATOR}h"
