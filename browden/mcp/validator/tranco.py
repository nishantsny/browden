"""Tranco top-sites membership — the popularity option of the read allowlist.

Tranco (https://tranco-list.eu) is a research-grade ranking of the most-visited
domains, hardened against the day-to-day churn and manipulation that skew raw
popularity lists. We bundle an offline snapshot of the top 100 000 registrable
domains (``configs/data/tranco-top-100k.txt.gz``) and treat membership as a
coarse "this is an established site" signal for the read gate. Popularity is a
proxy for *established*, never a guarantee of *safe* — a reputable domain can
still serve attacker-controlled content (see the README).

The check is fully local: no network at request time, O(number-of-labels) set
lookups. A host counts as listed if it, or any of its parent domains down to the
registrable domain, is in the top-N — so ``mail.google.com`` is covered by
``google.com`` — while lookalikes like ``google.com.evil.com`` are not (the walk
never reaches a bare public suffix).
"""
import gzip
from functools import lru_cache
from pathlib import Path

from ...common.logger import logger

# validator/ -> mcp/ -> browden/, then configs/data/
_DATA_DIR = Path(__file__).resolve().parents[2] / "configs" / "data"
DEFAULT_TRANCO_PATH = _DATA_DIR / "tranco-top-100k.txt.gz"
BUNDLED_TOP_N = 100_000


def _canonical(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


@lru_cache(maxsize=8)
def _load(path_str: str, top_n: int) -> frozenset[str]:
    """Read the first ``top_n`` domains from the gzipped snapshot (memoized).

    Degrades to an empty set (with a warning) if the snapshot is missing, so an
    uninstalled/mispathed data file means "Tranco matches nothing" rather than a
    crash — the denylist and website_overrides still apply.
    """
    if top_n <= 0:
        return frozenset()
    path = Path(path_str)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            domains = []
            for i, line in enumerate(fh):
                if i >= top_n:
                    break
                domain = line.strip()
                if domain:
                    domains.append(domain)
        return frozenset(domains)
    except FileNotFoundError:
        logger.warning(f"Tranco snapshot not found at {path}; Tranco read-allowlisting is inert")
        return frozenset()


class TrancoList:
    """Membership test against the top-N Tranco registrable domains."""

    def __init__(self, top_n: int = BUNDLED_TOP_N, path: Path = DEFAULT_TRANCO_PATH):
        self._top_n = top_n
        self._domains = _load(str(path), top_n)

    def __len__(self) -> int:
        return len(self._domains)

    def contains(self, host: str) -> bool:
        """True if ``host`` or one of its parent domains is in the top-N.

        Walks from the full host down to the two-label registrable domain, so a
        listed ``google.com`` covers every ``*.google.com`` subdomain, but the
        walk stops before a one-label tail — a bare public suffix (``com``,
        ``co.uk``) is never treated as listed even if it appears in the data.
        """
        host = _canonical(host)
        if not host:
            return False
        labels = host.split(".")
        for i in range(len(labels) - 1):  # stop before the bare TLD
            if ".".join(labels[i:]) in self._domains:
                return True
        return False
