"""Unit tests for setup/fetch_tranco.py — the pure parsing + sizing pieces.

Network is never touched: the CSV parser is fed the committed N=50 response
fixture (test/fixtures/tranco-top-50.csv), and the id resolver a fake urlopen.
"""
import io
import sys
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parents[3] / "setup"
sys.path.insert(0, str(SETUP_DIR))
import fetch_tranco as ft  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "tranco-top-50.csv"


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# -- _read_cap_bytes: 50 MiB @ 1M, scaled linearly, 1 MiB floor (issue #69) ---

def test_read_cap_full_list_is_50_mib():
    assert ft._read_cap_bytes(1_000_000) == 50 * 1024 * 1024


def test_read_cap_scales_linearly():
    assert ft._read_cap_bytes(500_000) == 25 * 1024 * 1024


def test_read_cap_has_1_mib_floor():
    assert ft._read_cap_bytes(50) == 1 * 1024 * 1024
    assert ft._read_cap_bytes(0) == 1 * 1024 * 1024


# -- _domains_from_csv: parse the committed N=50 response ----------------------

def test_parses_committed_fixture_to_50_domains():
    domains = ft._domains_from_csv(_fixture_text(), 1_000_000, truncated=False)
    assert len(domains) == 50
    assert domains[0] == "google.com"             # rank column dropped, domain kept
    assert all(d == d.lower() for d in domains)   # lower-cased
    assert all("," not in d for d in domains)     # not the raw "rank,domain" row


def test_respects_top_n_cap():
    domains = ft._domains_from_csv(_fixture_text(), 10, truncated=False)
    assert len(domains) == 10
    assert domains[0] == "google.com"


def test_truncated_drops_partial_last_row():
    # A body cut mid-row: the final "3,clou" must not become a domain.
    text = "1,google.com\n2,cloudflare.com\n3,clou"
    assert ft._domains_from_csv(text, 100, truncated=False) == \
        ["google.com", "cloudflare.com", "clou"]          # last kept when not truncated
    assert ft._domains_from_csv(text, 100, truncated=True) == \
        ["google.com", "cloudflare.com"]                  # partial row dropped


# -- _resolve_list_id: reads the short id token, no network -------------------

def test_resolve_list_id_reads_token(monkeypatch):
    monkeypatch.setattr(ft.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(b"XN2NN\n"))
    assert ft._resolve_list_id() == "XN2NN"
