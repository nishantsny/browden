#!/usr/bin/env python3
"""Record data-snapshot provenance as a comment block in the allowlist config.

The read perimeter is defined by two fetched-at-setup data snapshots — the
Tranco top-sites list and the Public Suffix List — that are *not* committed
(licensing, staleness; see setup/fetch_tranco.py and setup/fetch_psl.py). That
makes a given install non-reproducible and un-auditable on its own: nothing
records *which* Tranco day or *which* PSL a perimeter was built from.

This module writes that provenance next to the perimeter it describes — a small
``checkpoints`` block at the bottom of ``allowlist.yaml``:

    # >>> browden provenance ... >>>
    # checkpoints:
    #   tranco_id: XN2NN
    #   tranco_checksum_sha256: <64 hex>
    #   pal_checksum_sha256: <64 hex>
    # <<< browden provenance <<<

It is written as YAML **comments**, deliberately: the allowlist schema treats any
unknown top-level key as a write action, so a real ``checkpoints:`` mapping would
fail validation. Comments are invisible to the loader but greppable by a human
(and by `tranco_id` → a reproducible `fetch_tranco.py --url` permalink).

stdlib-only on purpose: this runs from the fetch scripts, which run with any
Python before the venv exists.
"""
import hashlib
from pathlib import Path

# Must match onetime_setup.copy_config's destination filename.
ALLOWLIST_FILENAME = "allowlist.yaml"

CHECKSUM_ALGO = "sha256"

# Sentinels bound the auto-managed block so it can be found and rewritten in
# place without disturbing the rest of the (hand-edited) config.
_BEGIN = "# >>> browden provenance — auto-written by setup; do not edit by hand >>>"
_END = "# <<< browden provenance <<<"

# The keys we render, in this order, always (missing ones show as empty
# placeholders so the block's shape is stable across partial updates — a Tranco
# refresh leaves the PSL line intact and vice-versa).
_KNOWN_KEYS = ("tranco_id", "tranco_checksum_sha256", "pal_checksum_sha256")

_PREAMBLE = (
    "# Fingerprints the exact data snapshots that define this read perimeter, so\n"
    "# a given install is auditable and reproducible. tranco_id is the Tranco\n"
    "# daily list id — pin it with: fetch_tranco.py --url .../download/<id>/<n>.\n"
    "# The checksums are sha256 over the fetched snapshot contents.\n"
    "# checkpoints:"
)


def sha256_hex(data: bytes) -> str:
    """Hex sha256 of ``data`` — the checksum recorded for a fetched snapshot."""
    return hashlib.sha256(data).hexdigest()


def _parse_existing(lines: list[str]) -> tuple[int, int, dict[str, str]]:
    """Locate an existing provenance block and read its ``key: value`` entries.

    Returns ``(start, end, values)`` where ``start``/``end`` are the line indices
    of the sentinels (inclusive) or ``(-1, -1, {})`` when no block is present.
    """
    start = next((i for i, ln in enumerate(lines) if ln.startswith(_BEGIN)), -1)
    if start == -1:
        return -1, -1, {}
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == _END), -1)
    if end == -1:  # opening sentinel with no close — treat as absent, don't guess
        return -1, -1, {}
    values: dict[str, str] = {}
    seen_header = False
    for ln in lines[start + 1:end]:
        body = ln.lstrip("#").strip()
        # Only lines under the "checkpoints:" header are data entries; the prose
        # preamble above it also contains colons and must not be parsed as keys.
        if not seen_header:
            seen_header = body == "checkpoints:"
            continue
        if ":" in body:
            key, _, val = body.partition(":")
            if key.strip():
                values[key.strip()] = val.strip()
    return start, end, values


def _render(values: dict[str, str]) -> list[str]:
    """Render the full provenance block (sentinels included) for ``values``."""
    extras = [k for k in values if k not in _KNOWN_KEYS]
    keys = list(_KNOWN_KEYS) + sorted(extras)
    out = [_BEGIN, *_PREAMBLE.splitlines()]
    for key in keys:
        out.append(f"#   {key}: {values.get(key, '')}".rstrip())
    out.append(_END)
    return out


def update_checkpoints(allowlist_path: Path, updates: dict[str, str]) -> None:
    """Upsert ``updates`` into the provenance block of ``allowlist_path``.

    Merges with any existing block (so a Tranco refresh doesn't drop the PSL
    checksum, and vice-versa) and rewrites it at the bottom of the file.

    Best-effort by contract: provenance is a nicety, never worth failing a fetch
    or aborting setup over. A missing allowlist (e.g. a custom ``--out`` with no
    sibling config) is skipped quietly; any write error warns and returns.
    """
    try:
        if not allowlist_path.exists():
            print(f"[skip] {allowlist_path} not found — provenance not recorded")
            return
        text = allowlist_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        start, end, values = _parse_existing(lines)
        values.update({k: str(v) for k, v in updates.items()})
        block = _render(values)
        if start != -1:
            body = lines[:start] + block + lines[end + 1:]
        else:
            # Append after the config, separated by exactly one blank line.
            body = lines
            while body and body[-1].strip() == "":
                body.pop()
            body += ["", *block]
        allowlist_path.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"[ok]   recorded provenance in {allowlist_path}: {', '.join(sorted(updates))}")
    except Exception as e:  # never fatal — provenance is non-critical metadata
        print(f"[warn] could not record provenance in {allowlist_path} ({e})")
