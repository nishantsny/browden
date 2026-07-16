"""End-to-end for setup/fetch_tranco.py: the whole fetch pipeline against the
committed N=50 Tranco response, then the snapshot is consumed by the real read
gate's data layer.

No network: urlopen is faked to serve ``/top-1m-id`` (a test id) and the matching
``/download/<id>/50`` (the committed CSV fixture). Exercises id-resolution ->
download -> CSV parse -> gzip write -> TrancoList membership.
"""
import io
import sys
from pathlib import Path

import pytest

from browden.mcp.validator.tranco import TrancoList

SETUP_DIR = Path(__file__).resolve().parents[2] / "setup"
sys.path.insert(0, str(SETUP_DIR))
import fetch_tranco as ft  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tranco-top-50.csv"


@pytest.fixture
def fake_tranco(monkeypatch):
    """Fake urlopen: the id endpoint returns TESTID; the matching download the fixture."""
    fixture_bytes = FIXTURE.read_bytes()

    def fake_urlopen(url, timeout=None):
        if url == ft.TRANCO_ID_URL:
            return io.BytesIO(b"TESTID\n")
        if url == ft.TRANCO_DOWNLOAD_TEMPLATE.format(list_id="TESTID", count=50):
            return io.BytesIO(fixture_bytes)
        raise AssertionError(f"unexpected URL fetched: {url}")

    monkeypatch.setattr(ft.urllib.request, "urlopen", fake_urlopen)


def test_fetch_writes_snapshot_the_read_gate_can_use(fake_tranco, tmp_path):
    out = tmp_path / "tranco-top-400k.txt.gz"
    n = ft.fetch(50, out)          # url=None -> resolve id, build /download/<id>/50
    assert n == 50
    assert out.exists()

    # The snapshot is consumed by the real read-gate data layer, unchanged.
    tl = TrancoList(tranco_top_n=1_000_000, path=out)
    assert len(tl) == 50
    assert "google.com" in tl                  # rank 1 in the fixture
    assert "cloudflare.com" in tl
    assert "definitely-not-a-real-tranco-domain.invalid" not in tl
