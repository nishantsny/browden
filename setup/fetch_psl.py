#!/usr/bin/env python3
"""Fetch (or refresh) the Public Suffix List snapshot the read allowlist uses.

The read gate reduces a host to its registrable domain (eTLD+1) with the Public
Suffix List before testing Tranco membership, so a shared-hosting subdomain
(``evil.github.io``, ``bucket.s3.amazonaws.com``) can't inherit the provider's
rank. The list lives *next to your allowlist config* —
``<config-dir>/public_suffix_list.dat`` — fetched there by ``onetime_setup.py``
on first run and refreshed with this script. It is **not** committed (it is
MPL-2.0 data). If it is missing, the read gate falls back to publicsuffix2's
older bundled copy.

Usage (from the repo root, any Python):

    python3 setup/fetch_psl.py                       # -> ~/.browden
    python3 setup/fetch_psl.py --config-dir /etc/browden
"""
import argparse
import urllib.request
from pathlib import Path

# stdlib-only on purpose: this runs with any Python, before the venv exists.
# Keep PSL_FILENAME in sync with browden.mcp.validator.popularity (the consumer).
PSL_FILENAME = "public_suffix_list.dat"
PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
DEFAULT_CONFIG_DIR = Path("~/.browden")


def snapshot_path(config_dir: Path) -> Path:
    """Where the PSL snapshot lives for a given config dir (next to allowlist.yaml)."""
    return config_dir.expanduser() / PSL_FILENAME


def fetch(out_path: Path, url: str = PSL_URL) -> int:
    """Download the Public Suffix List to ``out_path``. Returns bytes written."""
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url, timeout=60) as resp:
        blob = resp.read()
    # Cheap integrity check: refuse an error page / truncated body masquerading
    # as the list, rather than write it and silently weaken the gate.
    if b"===BEGIN ICANN DOMAINS===" not in blob:
        raise ValueError("downloaded file does not look like the Public Suffix List")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(blob)
    print(f"Wrote {out_path} ({len(blob)} bytes)")
    return len(blob)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch/refresh the Public Suffix List next to the allowlist")
    ap.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                    help=f"config dir the snapshot is written into (default: {DEFAULT_CONFIG_DIR})")
    ap.add_argument("--out", type=Path, default=None,
                    help="explicit output .dat path (overrides --config-dir)")
    ap.add_argument("--url", default=PSL_URL,
                    help="Public Suffix List URL (default: publicsuffix.org)")
    args = ap.parse_args()
    out = args.out if args.out is not None else snapshot_path(args.config_dir)
    fetch(out, args.url)


if __name__ == "__main__":
    main()
