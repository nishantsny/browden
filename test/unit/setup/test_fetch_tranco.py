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


# -- _list_id_from_url: id recorded for a pinned permalink --------------------

def test_list_id_from_download_permalink():
    assert ft._list_id_from_url("https://tranco-list.eu/download/ABCDE/1000000") == "ABCDE"


def test_list_id_from_url_falls_back_to_whole_url():
    # Not a /download/<id>/<n> permalink — record the URL itself, still auditable.
    assert ft._list_id_from_url("https://example.com/my-list.csv") == \
        "https://example.com/my-list.csv"


# -- fetch: records provenance (list id + content checksum) into the allowlist -

def test_fetch_records_provenance(monkeypatch, tmp_path):
    import hashlib
    monkeypatch.setattr(ft.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(_fixture_text().encode("utf-8")))
    allow = tmp_path / "allowlist.yaml"
    allow.write_text("read: {}\n", encoding="utf-8")
    out = tmp_path / ft.TRANCO_FILENAME
    # Pin an explicit permalink so no id-resolution round-trip is needed.
    ft.fetch(1_000_000, out, url="https://tranco-list.eu/download/ABCDE/1000000",
             allowlist_path=allow)

    import checkpoints as cp
    _, _, vals = cp._parse_existing(allow.read_text(encoding="utf-8").splitlines())
    domains = ft._domains_from_csv(_fixture_text(), 1_000_000, truncated=False)
    expected = hashlib.sha256(("\n".join(domains) + "\n").encode("utf-8")).hexdigest()
    assert vals["tranco_id"] == "ABCDE"
    assert vals["tranco_checksum_sha256"] == expected


def test_fetch_without_allowlist_path_skips_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(ft.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(_fixture_text().encode("utf-8")))
    out = tmp_path / ft.TRANCO_FILENAME
    n = ft.fetch(1_000_000, out, url="https://tranco-list.eu/download/ABCDE/1000000")
    assert n == 50 and out.exists()   # snapshot still written; no allowlist touched
