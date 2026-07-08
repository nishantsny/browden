"""Tests for the PSL-aware popularity allowlist.

``PopularityAllowlist`` owns a ``TrancoList`` + the Public Suffix List and answers
the read gate's question: is a *host* allowed (its registrable domain, eTLD+1, is
a listed top site)?

The unit conftest pins the default PSL at publicsuffix2's bundled list (all real
TLDs). Tests that need a *fresh* PSL write their own next to the Tranco snapshot
via ``_config``.
"""
import gzip
import shutil

from browden.mcp.validator import PopularityAllowlist
from browden.mcp.validator import popularity
from browden.mcp.validator.popularity import PSL_FILENAME
from browden.mcp.validator.tranco import TRANCO_FILENAME


def _snapshot(dir_, domains):
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
        shutil.copy(popularity._BUNDLED_PSL_PATH, dir_ / PSL_FILENAME)
    else:
        (dir_ / PSL_FILENAME).write_text("\n".join(psl_rules) + "\n", encoding="utf-8")
    return snap


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


def test_psl_filename():
    assert PSL_FILENAME == "public_suffix_list.dat"
