"""Tests for the Tranco data list and the PSL-aware popularity allowlist.

``TrancoList`` is pure data — exact membership over registrable domains.
``PopularityAllowlist`` owns a ``TrancoList`` plus the Public Suffix List and
answers the read gate's question: is a *host* allowed (its registrable domain,
eTLD+1, is a listed top site)? The split is what the two test groups below pin.

The unit conftest pins the default PSL at publicsuffix2's bundled snapshot (all
real TLDs). Tests that need a *fresh* PSL write their own next to the Tranco
snapshot via ``_config``.
"""
import gzip
import shutil

import pytest

from browden.mcp.validator import PopularityAllowlist, TrancoList
from browden.mcp.validator import tranco
from browden.mcp.validator.tranco import DEFAULT_TOP_N, PSL_FILENAME, TRANCO_FILENAME


def _snapshot(dir_, domains):
    """Write a gzipped Tranco snapshot of ``domains`` and return its path."""
    p = dir_ / TRANCO_FILENAME
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(domains) + "\n")
    return p


def _config(dir_, domains, psl_rules=None):
    """A Tranco snapshot + a sibling PSL (the bundled list, or ``psl_rules``).

    Returns the snapshot path; ``PopularityAllowlist(path=...)`` loads the PSL
    from the sibling ``public_suffix_list.dat``, so this keeps tests warning-free
    and lets a test supply a bespoke PSL to prove the fresh file is what's used.
    """
    snap = _snapshot(dir_, domains)
    if psl_rules is None:
        shutil.copy(tranco._BUNDLED_PSL_PATH, dir_ / PSL_FILENAME)
    else:
        (dir_ / PSL_FILENAME).write_text("\n".join(psl_rules) + "\n", encoding="utf-8")
    return snap


# == TrancoList: pure, exact membership over registrable domains ==============

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


# == PopularityAllowlist: PSL-aware, host -> registrable domain -> Tranco ======

def test_lists_top_sites_and_their_subdomains():
    pa = PopularityAllowlist(tranco_top_n=100)
    assert pa.contains("google.com")
    assert pa.contains("mail.google.com")      # subdomain reduces to google.com
    assert pa.contains("a.b.google.com")
    assert pa.contains("WWW.Google.COM")       # www + case canonicalized
    assert not pa.contains("google.com.evil.co")  # lookalike -> evil.co, not google.com
    assert not pa.contains("notgoogle.com")
    assert not pa.contains("nonexistent-xyz-987654.test")


def test_bare_public_suffix_and_empty_never_match(tmp_path):
    pa = PopularityAllowlist(tranco_top_n=10, path=_config(tmp_path, ["bbc.co.uk"]))
    assert pa.contains("bbc.co.uk")
    assert pa.contains("news.bbc.co.uk")   # subdomain covered
    assert not pa.contains("co.uk")        # a bare public suffix has no registrable domain
    assert not pa.contains("com")
    assert not pa.contains("")


def test_shared_hosting_subdomains_are_not_covered(tmp_path):
    # The provider's registrable/suffix domain is listed, but attacker-controlled
    # subdomains under it must NOT inherit the allow (finding H1). github.io,
    # s3.amazonaws.com and blogspot.com are on the bundled PSL.
    snap = _config(tmp_path, ["google.com", "github.io", "amazonaws.com", "blogspot.com"])
    pa = PopularityAllowlist(tranco_top_n=10, path=snap)
    assert pa.contains("mail.google.com")             # a real subdomain still works
    assert not pa.contains("evil.github.io")          # GitHub Pages
    assert not pa.contains("bucket.s3.amazonaws.com")  # S3 bucket
    assert not pa.contains("attacker.blogspot.com")   # a blog


def test_psl_supplement_blocks_non_psl_multitenant_hosts(tmp_path):
    # wordpress.com / sharepoint.com / storage.googleapis.com are multi-tenant but
    # NOT on the PSL — _PSL_SUPPLEMENT is injected so they still can't wildcard.
    snap = _config(tmp_path, ["wordpress.com", "sharepoint.com", "storage.googleapis.com"])
    pa = PopularityAllowlist(tranco_top_n=10, path=snap)
    assert not pa.contains("evil.wordpress.com")
    assert not pa.contains("tenant.sharepoint.com")
    assert not pa.contains("bucket.storage.googleapis.com")


def test_psl_is_loaded_from_the_snapshot_sibling(tmp_path):
    # A bespoke suffix the bundled PSL can't contain proves the *fresh* snapshot
    # PSL (next to the Tranco file) is what gets used — not publicsuffix2's copy.
    snap = _config(tmp_path, ["listed.example"],
                   psl_rules=["example", "dev", "bespoke-host.dev"])
    pa = PopularityAllowlist(tranco_top_n=10, path=snap)
    assert pa.contains("listed.example")
    assert pa.contains("sub.listed.example")
    assert not pa.contains("evil.bespoke-host.dev")  # reduces to itself, not listed


def test_missing_psl_falls_back_to_bundled(tmp_path):
    # No PSL beside the snapshot -> publicsuffix2's bundled list (which has
    # github.io), logged as a warning. The gate still functions.
    snap = _snapshot(tmp_path, ["google.com", "github.io"])
    pa = PopularityAllowlist(tranco_top_n=10, path=snap)
    assert pa.contains("mail.google.com")
    assert not pa.contains("evil.github.io")


def test_default_construction_uses_the_fixtures():
    pa = PopularityAllowlist()
    assert len(pa) > 0
    assert pa.contains("google.com")
    assert pa.contains("mail.google.com")


def test_filenames_and_default_top_n():
    # The names the fetchers write and the read gate reads must agree.
    assert TRANCO_FILENAME == "tranco-top-400k.txt.gz"
    assert PSL_FILENAME == "public_suffix_list.dat"
    assert DEFAULT_TOP_N == 1_000_000
