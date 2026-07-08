from urllib.parse import urlparse, urlunparse

from ...common.logger import logger
from .allowlist import Allowlist, ReadPolicy
from .errors import ValidationError

# Fallback when the gate carries no scheme policy of its own — e.g. a write-action
# :class:`Allowlist` (an anchor-click target). Default-secure: https only.
_DEFAULT_SCHEMES = frozenset({"https"})


def validate_url(url: str, allowlist: "Allowlist | ReadPolicy") -> str:
    """Normalize and gate a navigate-target URL against a host/path gate.

    ``allowlist`` is anything with ``is_allowed(host, path)`` — an
    :class:`Allowlist` (a single write-action section) or the read
    :class:`ReadPolicy` (denylist + Tranco + overrides). A URL is accepted iff
    its scheme is permitted *and* that gate allows its ``(host, path)``.

    The scheme is checked first, against ``allowlist.allowed_schemes`` (a
    :class:`ReadPolicy` carries the operator's ``read.schemes``; anything else
    defaults to https-only). This is what stops ``file://localhost/etc/passwd``
    and ``ftp://…`` from slipping through on a permissive host rule — the scheme
    gate is independent of, and applied before, the host allowlist.

    For allowed URLs, query strings and fragments are preserved unchanged — so
    '?', '#', '&' and spaces pass through.
    """
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    scheme = p.scheme.lower()
    allowed_schemes = getattr(allowlist, "allowed_schemes", _DEFAULT_SCHEMES)
    if scheme not in allowed_schemes:
        logger.warning(f"URL scheme blocked: {scheme!r} in {url!r} "
                       f"(allowed: {sorted(allowed_schemes)})")
        raise ValidationError(
            f"URL scheme not allowed: {scheme!r} (allowed: {sorted(allowed_schemes)})")
    # http(s) et al. must name a host; file:// legitimately has no authority
    # (file:///etc/passwd) and is gated on its path against the allowlist below.
    if scheme != "file" and not p.netloc:
        logger.warning(f"URL validation failed: no host in {url!r}")
        raise ValidationError(f"Invalid URL (no host): {url}")
    if not allowlist.is_allowed(p.hostname or "", p.path):
        logger.warning(f"URL blocked by allowlist: {p.hostname}{p.path}")
        raise ValidationError(f"URL not on allowlist: {p.hostname}{p.path}")
    logger.info(f"URL allowed: {url!r}")
    return urlunparse(p)
