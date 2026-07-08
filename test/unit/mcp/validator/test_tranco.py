"""Tests for the Tranco data list — exact membership over registrable domains.

``TrancoList`` is pure data (no PSL). The PSL-aware host reduction lives in
``PopularityAllowlist`` and is covered by test_popularity.py.
"""
import gzip

from browden.mcp.validator import TrancoList
from browden.mcp.validator.tranco import DEFAULT_TOP_N, TRANCO_FILENAME


def _snapshot(dir_, domains):
    """Write a gzipped Tranco snapshot of ``domains`` and return its path."""
    p = dir_ / TRANCO_FILENAME
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(domains) + "\n")
    return p


def test_tranco_membership_is_exact_no_reduction():
    tl = TrancoList()  # conftest fixture: google.com #1, cloudflare.com #2, …
    assert "google.com" in tl
    assert "cloudflare.com" in tl
    assert "mail.google.com" not in tl   # a subdomain is NOT reduced here
    assert "notgoogle.com" not in tl


def test_tranco_top_n_is_a_cutoff():
    assert "cloudflare.com" in TrancoList(tranco_top_n=100)   # rank #2 kept
    assert "cloudflare.com" not in TrancoList(tranco_top_n=1)  # only rank #1
    assert "google.com" in TrancoList(tranco_top_n=1)


def test_tranco_zero_top_n_is_empty():
    tl = TrancoList(tranco_top_n=0)
    assert len(tl) == 0
    assert "google.com" not in tl


def test_tranco_missing_snapshot_degrades_to_empty(tmp_path):
    tl = TrancoList(tranco_top_n=100, path=tmp_path / "absent.txt.gz")
    assert len(tl) == 0
    assert "google.com" not in tl


def test_tranco_custom_snapshot(tmp_path):
    tl = TrancoList(tranco_top_n=10, path=_snapshot(tmp_path, ["example.com", "bbc.co.uk"]))
    assert "example.com" in tl
    assert "bbc.co.uk" in tl
    assert "google.com" not in tl


def test_filename_and_default_top_n():
    # The name the fetcher writes and the read gate reads must agree.
    assert TRANCO_FILENAME == "tranco-top-400k.txt.gz"
    assert DEFAULT_TOP_N == 1_000_000
