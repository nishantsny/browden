"""Tranco top-sites membership — the popularity option of the read allowlist.

Tranco (https://tranco-list.eu) is a research-grade ranking of the most-visited
domains, hardened against the day-to-day churn and manipulation that skew raw
popularity lists. We keep an offline snapshot of the top 500 000 registrable
domains as ``tranco-top-400k.txt.gz`` *next to the allowlist config* — fetched
there by ``setup`` on first run (``setup/fetch_tranco.py``), never committed —
and treat membership as a coarse "this is an established site" signal for the
read gate. Popularity is a proxy for *established*, never a guarantee of *safe*
— a reputable domain can still serve attacker-controlled content (see README).

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

TRANCO_FILENAME = "tranco-top-400k.txt.gz"
DEFAULT_TOP_N = 1_000_000
# The snapshot lives next to the allowlist config; the loader passes that
# sibling path in. This is only the fallback for constructions that don't know
# a config dir (e.g. a bare ActionAllowlist(dict)) — the standard ~/.browden.
DEFAULT_TRANCO_PATH = (Path("~/.browden") / TRANCO_FILENAME).expanduser()

# Registrable domains and public suffixes that host mutually-untrusted tenants:
# anyone can publish arbitrary content at ``<anything>.<suffix>``. A membership
# hit on one of these must therefore vouch ONLY for the exact host, never as an
# ancestor of a subdomain — otherwise an attacker-controlled ``evil.<suffix>``
# inherits the "established site" allow (finding H1). Verified present, high in
# the shipped Tranco top-500k: amazonaws.com (#8), github.io (#116),
# workers.dev (#84), translate.goog (an open proxy to any origin), etc. Not
# exhaustive — a full Public Suffix List is the proper follow-up — but this
# neutralizes the high-rank offenders that would otherwise wildcard the web. A
# specific site under one of these is still reachable via a website_overrides
# entry (which is consulted before Tranco).
_MULTITENANT_SUFFIXES = frozenset({
    # object storage / CDNs — every bucket / distribution is attacker-controllable
    "amazonaws.com", "s3.amazonaws.com", "cloudfront.net", "googleusercontent.com",
    "storage.googleapis.com", "r2.dev", "blob.core.windows.net",
    # PaaS / static hosting where anyone can deploy a subdomain
    "workers.dev", "pages.dev", "web.app", "firebaseapp.com", "netlify.app",
    "vercel.app", "herokuapp.com", "azurewebsites.net", "github.io",
    "glitch.me", "repl.co", "translate.goog",
    # blog / site builders / per-tenant SaaS
    "blogspot.com", "wordpress.com", "wixsite.com", "weebly.com",
    "myshopify.com", "sharepoint.com",
})


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

    def __init__(self, top_n: int = DEFAULT_TOP_N, path: Path | None = None):
        # path=None resolves the module-level default at call time (not at
        # def time), so tests can repoint DEFAULT_TRANCO_PATH at a fixture.
        self._top_n = top_n
        self._domains = _load(str(path if path is not None else DEFAULT_TRANCO_PATH), top_n)

    def __len__(self) -> int:
        return len(self._domains)

    def contains(self, host: str) -> bool:
        """True if ``host`` or one of its parent domains is in the top-N.

        Walks from the full host down to the two-label registrable domain, so a
        listed ``google.com`` covers every ``*.google.com`` subdomain, but the
        walk stops before a one-label tail — a bare public suffix (``com``,
        ``co.uk``) is never treated as listed even if it appears in the data.

        A listed :data:`_MULTITENANT_SUFFIXES` host (shared hosting / public
        suffix) covers only *itself*, never its subdomains — otherwise an
        attacker-controlled ``evil.blogspot.com`` / ``x.s3.amazonaws.com`` would
        inherit the allow (finding H1). A more-specific subdomain that is itself
        listed still matches, because it is checked first on the walk down.
        """
        host = canonical_host(host)
        if not host:
            return False
        labels = host.split(".")
        for i in range(len(labels) - 1):  # stop before the bare TLD
            candidate = ".".join(labels[i:])
            if candidate in self._domains:
                # A multi-tenant suffix reached as an ANCESTOR (i != 0) does not
                # vouch for the subdomain beneath it; deny rather than wildcard.
                if i != 0 and candidate in _MULTITENANT_SUFFIXES:
                    return False
                return True
        return False
