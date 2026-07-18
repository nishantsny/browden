#!/usr/bin/env python3
"""Fetch (or refresh) the Tranco top-sites snapshot the read allowlist uses.

The read allowlist's Tranco option matches hosts against a snapshot that lives
*next to your allowlist config* — ``<config-dir>/tranco-top-400k.txt.gz``, the
top-N most-visited registrable domains, held offline so there is no network call
at request time. The file is **not** committed to the repo; ``onetime_setup.py``
fetches it on first run, and you re-run this script to refresh it.

Usage (from the repo root, any Python):

    python3 setup/fetch_tranco.py                        # top 1m -> ~/.browden
    python3 setup/fetch_tranco.py --top-n 500000         # a smaller cutoff
    python3 setup/fetch_tranco.py --config-dir /etc/browden

Tranco (https://tranco-list.eu) publishes a daily,
manipulation-resistant ranking behind a stable per-list id. We resolve the
current id from ``/top-1m-id`` and download the first ``--top-n`` rows of that
list as CSV from ``/download/<id>/<top-n>`` — ``rank,domain`` rows, of which we
keep the domain, lower-cased, one per line, gzipped. Pinning the id keeps a run
reproducible: the same id yields the same list. (Tranco documents a
``/api/lists/id/<id>`` metadata endpoint, but the *list data* is served from
``/download/<id>/<n>``.) Popularity is a proxy for "established", never a
guarantee of "safe" — see the README.

The download is size-bounded (see ``_read_cap_bytes``) so a wrong/hostile URL
can't stream unbounded data into memory.

Licensing / reproducibility: the daily list may aggregate CC BY-NC
(non-commercial) and CC BY-SA sources. For commercial use — or a specific,
permissively-licensed list — build one at https://tranco-list.eu/configure and
pass its permalink, e.g. ``--url https://tranco-list.eu/download/<LIST_ID>/1000000``.
See the README's Attribution section.
"""
import argparse
import csv
import gzip
import re
import urllib.request
from pathlib import Path

from checkpoints import ALLOWLIST_FILENAME, sha256_hex, update_checkpoints

# stdlib-only on purpose: this runs with any Python, before the venv exists.
# These must match browden.mcp.validator.tranco (the read gate that consumes
# the file) — keep them in sync if either changes.
TRANCO_FILENAME = "tranco-top-400k.txt.gz"
DEFAULT_TOP_N = 1_000_000

# The current daily list's id (a short token, e.g. "XN2NN"), and the CSV data
# for the first ``count`` rows of a given list.
TRANCO_ID_URL = "https://tranco-list.eu/top-1m-id"
TRANCO_DOWNLOAD_TEMPLATE = "https://tranco-list.eu/download/{list_id}/{count}"

# Upper bound on the download read: ~50 MiB for the full 1M list, scaled linearly
# by --top-n, never below 1 MiB (issue #69) — a safety bound against a wrong or
# hostile URL streaming unbounded data, not a tight fit (the real 1M CSV is well
# under 50 MiB).
_MAX_READ_BYTES = 50 * 1024 * 1024
_MIN_READ_BYTES = 1 * 1024 * 1024

DEFAULT_CONFIG_DIR = Path("~/.browden")


def snapshot_path(config_dir: Path) -> Path:
    """Where the snapshot lives for a given config dir (next to allowlist.yaml)."""
    return config_dir.expanduser() / TRANCO_FILENAME


def _read_cap_bytes(top_n: int) -> int:
    """Max bytes to read for ``top_n`` rows: 50 MiB at 1M, scaled down, 1 MiB floor."""
    scaled = _MAX_READ_BYTES * max(top_n, 0) // DEFAULT_TOP_N
    return max(_MIN_READ_BYTES, scaled)


def _domains_from_csv(text: str, top_n: int, *, truncated: bool) -> list[str]:
    """Parse ``rank,domain`` CSV text into up to ``top_n`` lower-cased domains.

    When ``truncated`` (the read hit its byte cap) the final line may be a partial
    row, so it is dropped before parsing rather than risk keeping a cut-off domain.
    """
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[:-1]
    domains: list[str] = []
    for row in csv.reader(lines):
        if len(row) >= 2 and row[1].strip():
            domains.append(row[1].strip().lower())
        if len(domains) >= top_n:
            break
    return domains


def _resolve_list_id(url: str = TRANCO_ID_URL) -> str:
    """Fetch the current daily list's id (a short token) from ``/top-1m-id``."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        list_id = resp.read(256).decode("utf-8").strip()
    if not list_id:
        raise RuntimeError(f"empty Tranco list id from {url}")
    return list_id


def _list_id_from_url(url: str) -> str:
    """The list id recorded for a permalink ``.../download/<id>/<count>``.

    Falls back to the whole URL when it isn't a recognizable download permalink,
    so provenance still points at *something* reproducible for a custom ``--url``.
    """
    m = re.search(r"/download/([^/]+)/\d+", url)
    return m.group(1) if m else url


def fetch(top_n: int, out_path: Path, url: str | None = None,
          allowlist_path: Path | None = None) -> int:
    """Write the first ``top_n`` Tranco domains to ``out_path`` (gzipped, one per line).

    With no ``url`` the current list id is resolved from ``/top-1m-id`` and the
    CSV is pulled from ``/download/<id>/<top_n>``; pass an explicit ``url`` to pin
    a specific list permalink instead. The response body is read under a
    size cap (:func:`_read_cap_bytes`).

    When ``allowlist_path`` is given, records the resolved list id and a sha256
    of the domain-list content into that file's provenance block (best-effort).
    """
    if url is None:
        list_id = _resolve_list_id()
        url = TRANCO_DOWNLOAD_TEMPLATE.format(list_id=list_id, count=top_n)
    else:
        list_id = _list_id_from_url(url)
    cap = _read_cap_bytes(top_n)
    print(f"Downloading {url} (up to {cap} bytes) ...")
    with urllib.request.urlopen(url, timeout=120) as resp:
        blob = resp.read(cap)
    truncated = len(blob) >= cap
    if truncated:
        print(f"WARNING: response reached the {cap}-byte cap; dropping a possibly-partial last row")
    domains = _domains_from_csv(blob.decode("utf-8", errors="replace"), top_n, truncated=truncated)
    if not domains:
        raise RuntimeError(f"no domains parsed from {url} — unexpected response format")
    # Checksum the uncompressed content, not the .gz: gzip stamps an mtime, so
    # identical domains would hash differently run-to-run. The content is the
    # perimeter — hashing it is what makes the checkpoint reproducible.
    content = "\n".join(domains) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(content)
    print(f"Wrote {len(domains)} domains to {out_path} ({out_path.stat().st_size} bytes)")
    if allowlist_path is not None:
        update_checkpoints(allowlist_path, {
            "tranco_id": list_id,
            "tranco_checksum_sha256": sha256_hex(content.encode("utf-8")),
        })
    return len(domains)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch/refresh the Tranco snapshot next to the allowlist")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                    help=f"how many top domains to keep (default: {DEFAULT_TOP_N})")
    ap.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                    help=f"config dir the snapshot is written into (default: {DEFAULT_CONFIG_DIR})")
    ap.add_argument("--out", type=Path, default=None,
                    help="explicit output .txt.gz path (overrides --config-dir)")
    ap.add_argument("--url", default=None,
                    help="pin an explicit Tranco list CSV permalink (e.g. "
                         "https://tranco-list.eu/download/<LIST_ID>/1000000); "
                         "default resolves the current daily list id automatically")
    args = ap.parse_args()
    out = args.out if args.out is not None else snapshot_path(args.config_dir)
    allowlist = args.config_dir.expanduser() / ALLOWLIST_FILENAME
    fetch(args.top_n, out, args.url, allowlist_path=allowlist)


if __name__ == "__main__":
    main()
