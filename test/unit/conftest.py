"""Unit-test fixtures shared across the suite.

The real Tranco snapshot is fetched by setup into the user config dir and is
never committed, so the unit tests can't read it. Instead we ship a tiny
committed fixture (``test/fixtures/tranco-mini.txt.gz``, the real top ~100
domains) and, for every unit test, repoint the Tranco default path at it. Tests
that build an allowlist from a dict or a fixture-less config fall through to
this default, so they see a small but real top-sites list (google.com #1,
cloudflare.com #2, …) with no network and full determinism.
"""
from pathlib import Path

import pytest

from browden.mcp.validator import tranco

TRANCO_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tranco-mini.txt.gz"


@pytest.fixture(autouse=True)
def _tranco_fixture(monkeypatch):
    """Point the Tranco default snapshot at the committed mini fixture."""
    monkeypatch.setattr(tranco, "DEFAULT_TRANCO_PATH", TRANCO_FIXTURE)
    tranco._load.cache_clear()
    yield
    tranco._load.cache_clear()
