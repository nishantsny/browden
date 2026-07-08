"""Unit-test fixtures shared across the suite.

The real Tranco snapshot is fetched by setup into the user config dir and is
never committed, so the unit tests can't read it. Instead we ship a tiny
committed fixture (``test/fixtures/tranco-mini.txt.gz``, the real top ~100
domains) and, for every unit test, repoint the Tranco default path at it. Tests
that build an allowlist from a dict or a fixture-less config fall through to
this default, so they see a small but real top-sites list (google.com #1,
cloudflare.com #2, …) with no network and full determinism.

The read gate also reduces a host to its registrable domain via the Public
Suffix List. The fresh PSL setup fetches is likewise not committed, so we pin
the PSL default at publicsuffix2's *bundled* snapshot — it covers every real TLD
(so mini-fixture domains reduce correctly) and needs no network. Tests that must
exercise a *fresh* PSL place their own ``public_suffix_list.dat`` next to a
snapshot and construct against that path.
"""
from pathlib import Path

import pytest

from browden.mcp.validator import tranco

TRANCO_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tranco-mini.txt.gz"


@pytest.fixture(autouse=True)
def _tranco_fixture(monkeypatch):
    """Pin the default Tranco snapshot at the mini fixture and the default PSL at
    publicsuffix2's bundled list; clear both memoized loaders around each test."""
    monkeypatch.setattr(tranco, "DEFAULT_TRANCO_PATH", TRANCO_FIXTURE)
    monkeypatch.setattr(tranco, "DEFAULT_PSL_PATH", tranco._BUNDLED_PSL_PATH)
    tranco._load.cache_clear()
    tranco._psl.cache_clear()
    yield
    tranco._load.cache_clear()
    tranco._psl.cache_clear()
