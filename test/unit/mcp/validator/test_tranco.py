import gzip

import pytest

from browden.mcp.validator import TrancoList
from browden.mcp.validator.tranco import DEFAULT_TOP_N, TRANCO_FILENAME


def test_default_snapshot_loads_the_fixture():
    # conftest repoints the default path at the committed mini fixture, so the
    # no-arg default construction reads a small but real top-sites list.
    tl = TrancoList()
    assert len(tl) > 0
    assert tl.contains("google.com")


def test_top_ranked_sites_are_listed():
    tl = TrancoList(top_n=100)
    assert tl.contains("google.com")      # rank #1
    assert tl.contains("cloudflare.com")  # rank #2


def test_subdomains_are_covered_but_lookalikes_are_not():
    tl = TrancoList(top_n=100)
    assert tl.contains("mail.google.com")
    assert tl.contains("a.b.google.com")
    assert tl.contains("WWW.Google.COM")            # canonicalized (www + case)
    assert not tl.contains("google.com.evil.co")    # suffix spoof rejected
    assert not tl.contains("notgoogle.com")         # prefix spoof rejected


def test_top_n_is_a_cutoff():
    only_first = TrancoList(top_n=1)
    assert only_first.contains("google.com")
    assert not only_first.contains("cloudflare.com")  # rank #2 excluded


def test_bare_tld_never_matches():
    tl = TrancoList(top_n=1000)
    assert not tl.contains("com")
    assert not tl.contains("")


def test_zero_top_n_is_empty():
    tl = TrancoList(top_n=0)
    assert len(tl) == 0
    assert not tl.contains("google.com")


def test_missing_snapshot_degrades_to_empty(tmp_path):
    tl = TrancoList(top_n=100, path=tmp_path / "absent.txt.gz")
    assert len(tl) == 0
    assert not tl.contains("google.com")


def test_custom_snapshot_path(tmp_path):
    p = tmp_path / "mini.txt.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("example.com\nfoo.co.uk\n")
    tl = TrancoList(top_n=10, path=p)
    assert tl.contains("example.com")
    assert tl.contains("shop.foo.co.uk")  # subdomain of a 3-label registrable domain
    assert not tl.contains("co.uk")       # bare suffix not matched
    assert not tl.contains("google.com")


def test_snapshot_name_and_default_top_n():
    # The name the fetcher writes and the read gate reads must agree; the
    # snapshot is fetched into the config dir, so we do NOT assert it exists.
    assert TRANCO_FILENAME == "tranco-top-400k.txt.gz"
    assert DEFAULT_TOP_N == 400_000
