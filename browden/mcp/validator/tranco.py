"""Tranco top-sites membership — the popularity option of the read allowlist.

Tranco (https://tranco-list.eu) is a research-grade ranking of the most-visited
domains, hardened against the day-to-day churn and manipulation that skew raw
popularity lists. We keep an offline snapshot of the top 500 000 registrable
domains as ``tranco-top-400k.txt.gz`` *next to the allowlist config* — fetched
there by ``setup`` on first run (``setup/fetch_tranco.py``), never committed —
and treat membership as a coarse "this is an established site" signal for the
read gate. Popularity is a proxy for *established*, never a guarantee of *safe*
— a reputable domain can still serve attacker-controlled content (see README).

A host is matched by reducing it to its **registrable domain** (eTLD+1) via the
Public Suffix List, then testing that against the top-N. So ``mail.google.com``
reduces to ``google.com`` (a listed domain covers its own subdomains), while a
shared-hosting subdomain like ``evil.github.io`` or ``bucket.s3.amazonaws.com``
reduces to *itself* — because the PSL (private section included) marks
``github.io`` / ``s3.amazonaws.com`` as registration boundaries — and so is
allowed only if it ranks in its own right, never by inheriting the provider's
stature (finding H1). A lookalike ``google.com.evil.com`` reduces to ``evil.com``.

The check is fully local: no network at request time.
"""
import gzip
from functools import lru_cache
from pathlib import Path

import publicsuffix2

from ...common.logger import logger

TRANCO_FILENAME = "tranco-top-400k.txt.gz"
PSL_FILENAME = "public_suffix_list.dat"
DEFAULT_TOP_N = 1_000_000
# The snapshots live next to the allowlist config; the loader passes that
# sibling path in. These are only the fallback for constructions that don't know
# a config dir (e.g. a bare ActionAllowlist(dict)) — the standard ~/.browden.
DEFAULT_TRANCO_PATH = (Path("~/.browden") / TRANCO_FILENAME).expanduser()
DEFAULT_PSL_PATH = (Path("~/.browden") / PSL_FILENAME).expanduser()

# publicsuffix2 ships its own (older) PSL snapshot; we use it only as a fallback.
_BUNDLED_PSL_PATH = Path(publicsuffix2.__file__).resolve().parent / PSL_FILENAME

# Multi-tenant hosts that are NOT on the Public Suffix List (verified against the
# current list), so the PSL alone would let ``<anything>.<suffix>`` inherit the
# suffix's Tranco rank. We add them as extra suffix rules so those subdomains
# reduce to themselves and are gated individually, same as any PSL entry. Kept
# deliberately small — the PSL covers github.io, s3.amazonaws.com, workers.dev,
# pages.dev, vercel.app, blogspot.com, translate.goog, … — and only holds the
# genuine PSL absentees. (Escape hatch for a specific host under one of these:
# add a website_overrides entry, which is consulted before Tranco.)
_PSL_SUPPLEMENT = (
    "wordpress.com",            # <name>.wordpress.com blogs
    "googleusercontent.com",    # lh3.googleusercontent.com, *.googleusercontent user content
    "storage.googleapis.com",   # <bucket>.storage.googleapis.com
    "weebly.com",               # <name>.weebly.com sites
    "sharepoint.com",           # <tenant>.sharepoint.com
)


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


@lru_cache(maxsize=4)
def _psl(psl_path_str: str) -> "publicsuffix2.PublicSuffixList":
    """The Public Suffix List used to reduce a host to its registrable domain.

    Prefers the fresh snapshot ``setup`` fetches next to the config; falls back
    to publicsuffix2's *bundled* list (older — it can miss newer suffixes such as
    ``pages.dev`` / ``vercel.app``) with a warning. Either source is extended
    with :data:`_PSL_SUPPLEMENT` so PSL-absent multi-tenant hosts can't wildcard.
    Memoized: parsing the ~10k-rule list is not free.
    """
    path = Path(psl_path_str) if psl_path_str else None
    if path and path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        if path:
            logger.warning(
                f"PSL snapshot not found at {path}; using publicsuffix2's bundled "
                f"(older) list — run setup/fetch_psl.py to refresh")
        lines = _BUNDLED_PSL_PATH.read_text(encoding="utf-8").splitlines()
    return publicsuffix2.PublicSuffixList(psl_file=lines + list(_PSL_SUPPLEMENT))


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
    :class:`PopularityAllowlist`, which owns a ``TrancoList`` and does the
    reduction before consulting it.
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


class PopularityAllowlist:
    """Read-allowlist membership by popularity, PSL-aware — the Tranco read gate.

    Owns the two data sources the popularity gate needs: the :class:`TrancoList`
    (which registrable domains are established) and the Public Suffix List (where
    a host's registrable domain begins). It exposes the single decision the read
    policy asks of it — :meth:`contains` — reducing a host to its registrable
    domain (eTLD+1) via the PSL and testing that against the Tranco top-N.
    """

    def __init__(self, tranco_top_n: int = DEFAULT_TOP_N, path: Path | None = None):
        # `path` is the Tranco snapshot; the PSL snapshot sits beside it (same
        # config dir). path=None falls back to the module defaults (tests repoint
        # them); a missing PSL degrades to publicsuffix2's bundled list.
        self._tranco = TrancoList(tranco_top_n=tranco_top_n, path=path)
        psl_path = (path.parent / PSL_FILENAME) if path is not None else DEFAULT_PSL_PATH
        self._psl = _psl(str(psl_path))

    def __len__(self) -> int:
        return len(self._tranco)

    def contains(self, host: str) -> bool:
        """True iff the host's registrable domain (eTLD+1) is in the Tranco top-N.

        The host is reduced to its registrable domain via the Public Suffix List
        (private section + our supplement), so ``mail.google.com`` -> ``google.com``
        (a listed domain covers its subdomains), while a shared-hosting subdomain
        ``evil.github.io`` / ``bucket.s3.amazonaws.com`` reduces to *itself* and so
        is listed only if it ranks on its own — it never inherits the provider's
        rank (finding H1). ``google.com.evil.com`` reduces to ``evil.com``.
        """
        host = canonical_host(host)
        if not host:
            return False
        registrable = self._psl.get_sld(host)
        return registrable is not None and registrable in self._tranco
