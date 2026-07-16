"""The PSL-aware popularity read allowlist.

:class:`PopularityAllowlist` owns the two data sources the read gate's popularity
option needs — the Tranco top-N (:class:`~browden.mcp.validator.tranco.TrancoList`)
and the Public Suffix List — and answers the one question the read policy asks of
it: is a host allowed? A host is reduced to its registrable domain (eTLD+1) via
the PSL and tested against Tranco, so ``mail.google.com`` -> ``google.com`` is
covered while a shared-hosting subdomain ``evil.github.io`` /
``bucket.s3.amazonaws.com`` reduces to *itself* and is allowed only if it ranks on
its own — never by inheriting the provider's stature (finding H1). A lookalike
``google.com.evil.com`` reduces to ``evil.com``.

Fully local: the PSL is an offline snapshot fetched next to the config
(``setup/fetch_psl.py``), falling back to publicsuffix2's bundled list.
"""
from functools import lru_cache
from pathlib import Path

import publicsuffix2

from ...common.logger import logger
from .tranco import DEFAULT_TOP_N, TrancoList, canonical_host

PSL_FILENAME = "public_suffix_list.dat"
# The snapshot sits next to the Tranco snapshot; PopularityAllowlist derives it
# from the passed Tranco path. This is the fallback for path-less constructions.
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

    @lru_cache(maxsize=64)
    def contains(self, host: str) -> bool:
        """True iff the host's registrable domain (eTLD+1) is in the Tranco top-N.

        The host is reduced to its registrable domain via the Public Suffix List
        (private section + our supplement), so ``mail.google.com`` -> ``google.com``
        (a listed domain covers its subdomains), while a shared-hosting subdomain
        ``evil.github.io`` / ``bucket.s3.amazonaws.com`` reduces to *itself* and so
        is listed only if it ranks on its own — it never inherits the provider's
        rank (finding H1). ``google.com.evil.com`` reduces to ``evil.com``. Memoized
        per (instance, host): the instance is immutable after construction.
        """
        host = canonical_host(host)
        if not host:
            return False
        registrable = self._psl.get_sld(host)
        return registrable is not None and registrable in self._tranco
