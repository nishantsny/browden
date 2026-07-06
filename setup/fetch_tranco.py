#!/usr/bin/env python3
"""Refresh the bundled Tranco top-sites snapshot the read allowlist uses.

The read allowlist's Tranco option (see configs/samples/allowlist-read-deny.yaml)
matches hosts against ``browden/configs/data/tranco-top-100k.txt.gz`` — the
top-N most-visited registrable domains, held offline so there is no network call
at request time. This script regenerates that file from the latest Tranco list.

Usage (from the repo root, any Python):

    python3 setup/fetch_tranco.py            # top 100k (the bundled default)
    python3 setup/fetch_tranco.py --top-n 200000

Tranco (https://tranco-list.eu) publishes a manipulation-resistant ranking; the
daily "top-1m" download is a zip of ``rank,domain`` CSV rows. We keep the first
--top-n domains, lower-cased, one per line, gzipped. Popularity is a proxy for
"established", never a guarantee of "safe" — see the README.
"""
import argparse
import csv
import gzip
import io
import urllib.request
import zipfile
from pathlib import Path

TRANCO_ZIP_URL = "https://tranco-list.eu/top-1m.csv.zip"
OUT_PATH = Path(__file__).resolve().parents[1] / "browden" / "configs" / "data" / "tranco-top-100k.txt.gz"


def fetch(top_n: int, url: str, out_path: Path) -> int:
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url, timeout=120) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = zf.namelist()[0]
        rows = csv.reader(io.TextIOWrapper(zf.open(name), encoding="utf-8"))
        domains = []
        for row in rows:
            if len(row) >= 2 and row[1].strip():
                domains.append(row[1].strip().lower())
            if len(domains) >= top_n:
                break
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write("\n".join(domains) + "\n")
    print(f"Wrote {len(domains)} domains to {out_path} ({out_path.stat().st_size} bytes)")
    return len(domains)


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh the bundled Tranco snapshot")
    ap.add_argument("--top-n", type=int, default=100_000, help="how many top domains to keep (default: 100000)")
    ap.add_argument("--url", default=TRANCO_ZIP_URL, help="Tranco top-1m zip URL")
    ap.add_argument("--out", type=Path, default=OUT_PATH, help="output .txt.gz path")
    args = ap.parse_args()
    fetch(args.top_n, args.url, args.out)


if __name__ == "__main__":
    main()
