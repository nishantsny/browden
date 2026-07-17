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
from types import SimpleNamespace

import pytest

from browden.mcp.session_management import browser_session_store
from browden.mcp.validator import popularity, tranco
from browden.mcp.validator.popularity import PopularityAllowlist

TRANCO_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tranco-mini.txt.gz"


@pytest.fixture(autouse=True)
def _tranco_fixture(monkeypatch):
    """Pin the default Tranco snapshot at the mini fixture and the default PSL at
    publicsuffix2's bundled list; clear the memoized loaders (and the per-instance
    ``contains`` cache) around each test."""
    monkeypatch.setattr(tranco, "DEFAULT_TRANCO_PATH", TRANCO_FIXTURE)
    monkeypatch.setattr(popularity, "DEFAULT_PSL_PATH", popularity._BUNDLED_PSL_PATH)
    tranco._load.cache_clear()
    popularity._psl.cache_clear()
    PopularityAllowlist.contains.cache_clear()
    yield
    tranco._load.cache_clear()
    popularity._psl.cache_clear()
    PopularityAllowlist.contains.cache_clear()


@pytest.fixture(autouse=True)
def _no_store_atexit(monkeypatch):
    """Keep unit-test session stores from registering real atexit hooks.

    ``BrowserSessionStore.get_or_create_session`` registers an interpreter-exit
    shutdown for the sessions it creates. In unit tests those sessions wrap fake
    backends — there is no Chrome to tear down — so the hooks only fire after
    pytest's summary and log "MCP Server shutting down" lines into the terminal.
    Stub the registration (in the store module's namespace only); the real
    atexit path is covered by the e2e suite, which drives real browsers.
    """
    monkeypatch.setattr(
        browser_session_store, "atexit",
        SimpleNamespace(register=lambda *a, **k: None))
