from urllib.parse import urlparse, urlunparse

from ...common.logger import logger
from .allowlist import Allowlist, ReadPolicy
from .errors import ValidationError


def validate_url(url: str, allowlist: "Allowlist | ReadPolicy") -> str:
    """Normalize and gate a navigate-target URL against a host/path gate.

    ``allowlist`` is anything with ``is_allowed(host, path)`` — an
    :class:`Allowlist` (a single write-action section) or the read
    :class:`ReadPolicy` (denylist + Tranco + overrides). A URL is accepted iff
    that gate allows its ``(host, path)``. For allowed URLs, query strings and
    fragments are preserved unchanged — so '?', '#', '&' and spaces pass through.
    """
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        logger.warning(f"URL validation failed: no host in {url!r}")
        raise ValidationError(f"Invalid URL (no host): {url}")
    if not allowlist.is_allowed(p.hostname or "", p.path):
        logger.warning(f"URL blocked by allowlist: {p.hostname}{p.path}")
        raise ValidationError(f"URL not on allowlist: {p.hostname}{p.path}")
    logger.info(f"URL allowed: {url!r}")
    return urlunparse(p)
