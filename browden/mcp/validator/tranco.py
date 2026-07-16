"""Tranco top-sites membership — the popularity data behind the read allowlist.

Tranco (https://tranco-list.eu) is a research-grade ranking of the most-visited
domains, hardened against the day-to-day churn and manipulation that skew raw
popularity lists. We keep an offline snapshot of the top 500 000 registrable
domains as ``tranco-top-400k.txt.gz`` *next to the allowlist config* — fetched
there by ``setup`` on first run (``setup/fetch_tranco.py``), never committed —
and treat membership as a coarse "this is an established site" signal for the
read gate. Popularity is a proxy for *established*, never a guarantee of *safe*
— a reputable domain can still serve attacker-controlled content (see README).

This module holds only the data: :class:`TrancoList` (exact membership over
registrable domains) and the shared :func:`canonical_host`. Reducing an arbitrary
host to its registrable domain via the Public Suffix List and testing it there is
:class:`~browden.mcp.validator.popularity.PopularityAllowlist`'s job.

The check is fully local: no network at request time.
"""
import gzip
from functools import lru_cache
from pathlib import Path

from ...common.logger import logger

TRANCO_FILENAME = "tranco-top-400k.txt.gz"
DEFAULT_TOP_N = 1_000_000
# The snapshot lives next to the allowlist config; the loader passes that sibling
# path in. This is only the fallback for constructions that don't know a config
# dir (e.g. a bare ActionAllowlist(dict)) — the standard ~/.browden.
DEFAULT_TRANCO_PATH = (Path("~/.browden") / TRANCO_FILENAME).expanduser()


def canonical_host(host: str) -> str:
    """Normalize a host for policy comparison. THE one canonicalizer, shared.

    Strips surrounding whitespace, lower-cases, drops a trailing root dot, and
    removes a single leading ``www.``. The allowlist/denylist import this too
    (see ``allowlist.canonical_host``) so every gate compares hosts the way the
    browser resolves them — in particular ``evil.com.`` must canonicalize to
    ``evil.com``, or a trailing dot slips a denied host past the denylist while
    the browser still reaches it (finding H3).
    """
    host = host.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


@lru_cache(maxsize=8)
def _load(path_str: str, tranco_top_n: int) -> frozenset[str]:
    """Read the first ``tranco_top_n`` domains from the gzipped snapshot (memoized).

    Degrades to an empty set (with a warning) if the snapshot is missing, so an
    uninstalled/mispathed data file means "Tranco matches nothing" rather than a
    crash — the denylist and website_overrides still apply.
    """
    if tranco_top_n <= 0:
        return frozenset()
    path = Path(path_str)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            domains = []
            for i, line in enumerate(fh):
                if i >= tranco_top_n:
                    break
                domain = line.strip()
                if domain:
                    domains.append(domain)
        return frozenset(domains)
    except FileNotFoundError:
        logger.warning(f"Tranco snapshot not found at {path}; Tranco read-allowlisting is inert")
        return frozenset()


class TrancoList:
    """The Tranco top-N registrable domains as a fast membership set (data only).

    Pure data: it loads the snapshot and answers *exact* set membership over
    registrable domains (``google.com``, ``bbc.co.uk``). It deliberately does NOT
    reduce a host to its registrable domain — that PSL-aware step lives in
    :class:`~browden.mcp.validator.popularity.PopularityAllowlist`, which owns a
    ``TrancoList`` and does the reduction before consulting it.
    """

    def __init__(self, tranco_top_n: int = DEFAULT_TOP_N, path: Path | None = None):
        # path=None resolves the module-level default at call time (not at def
        # time), so tests can repoint DEFAULT_TRANCO_PATH at a fixture.
        self._top_n = tranco_top_n
        self._domains = _load(str(path if path is not None else DEFAULT_TRANCO_PATH), tranco_top_n)

    def __len__(self) -> int:
        return len(self._domains)

    def __contains__(self, registrable_domain: str) -> bool:
        """Exact membership: is ``registrable_domain`` one of the top-N entries?"""
        return registrable_domain in self._domains
