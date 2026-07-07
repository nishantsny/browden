#!/usr/bin/env python3
"""Fetch (or refresh) the Tranco top-sites snapshot the read allowlist uses.

The read allowlist's Tranco option matches hosts against a snapshot that lives
*next to your allowlist config* — ``<config-dir>/tranco-top-400k.txt.gz``, the
top-N most-visited registrable domains, held offline so there is no network call
at request time. The file is **not** committed to the repo; ``onetime_setup.py``
fetches it on first run, and you re-run this script to refresh it.

Usage (from the repo root, any Python):

    python3 setup/fetch_tranco.py                        # top 500k -> ~/.browden
    python3 setup/fetch_tranco.py --top-n 1000000        # the whole list
    python3 setup/fetch_tranco.py --config-dir /etc/browden

Tranco (https://tranco-list.eu) publishes a manipulation-resistant ranking; the
daily "top-1m" download is a zip of ``rank,domain`` CSV rows. We keep the first
--top-n domains, lower-cased, one per line, gzipped. Popularity is a proxy for
"established", never a guarantee of "safe" — see the README.

Licensing / reproducibility: the default ``--url`` is Tranco's daily combined
list, which may aggregate CC BY-NC (non-commercial) and CC BY-SA sources. For
commercial use — or just a stable, reproducible list — build one restricted to
permissively-licensed sources at https://tranco-list.eu/configure and pass its
permanent permalink, e.g.
``--url https://tranco-list.eu/download/<LIST_ID>/1000000``. See the README's
Attribution section.
"""
import argparse
import csv
import gzip
import io
import urllib.request
import zipfile
from pathlib import Path

# stdlib-only on purpose: this runs with any Python, before the venv exists.
# These must match browden.mcp.validator.tranco (the read gate that consumes
# the file) — keep them in sync if either changes.
TRANCO_FILENAME = "tranco-top-400k.txt.gz"
DEFAULT_TOP_N = 500_000
TRANCO_ZIP_URL = "https://tranco-list.eu/top-1m.csv.zip"
DEFAULT_CONFIG_DIR = Path("~/.browden")


def snapshot_path(config_dir: Path) -> Path:
    """Where the snapshot lives for a given config dir (next to allowlist.yaml)."""
    return config_dir.expanduser() / TRANCO_FILENAME


def fetch(top_n: int, out_path: Path, url: str = TRANCO_ZIP_URL) -> int:
    """Download the Tranco top-1m list and write the first ``top_n`` domains to
    ``out_path`` (gzipped, one lower-cased registrable domain per line)."""
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
    ap = argparse.ArgumentParser(description="Fetch/refresh the Tranco snapshot next to the allowlist")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                    help=f"how many top domains to keep (default: {DEFAULT_TOP_N})")
    ap.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                    help=f"config dir the snapshot is written into (default: {DEFAULT_CONFIG_DIR})")
    ap.add_argument("--out", type=Path, default=None,
                    help="explicit output .txt.gz path (overrides --config-dir)")
    ap.add_argument("--url", default=TRANCO_ZIP_URL,
                    help="Tranco list zip URL (default: the daily top-1m). Point "
                         "at a permanent list permalink to pin a specific, "
                         "permissively-licensed list — see the module docstring.")
    args = ap.parse_args()
    out = args.out if args.out is not None else snapshot_path(args.config_dir)
    fetch(args.top_n, out, args.url)


if __name__ == "__main__":
    main()
