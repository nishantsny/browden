from urllib.parse import urlparse, urlunparse

from ...common.logger import logger
from .allowlist import Allowlist
from .errors import ValidationError


def validate_url(url: str, allowlist: Allowlist) -> str:
    """Normalize and gate a navigate-target URL against the allowlist.

    A URL is accepted iff its (host, path) matches an entry in the allowlist.
    For allowed URLs, query strings and fragments are preserved unchanged —
    so '?', '#', '&' and spaces all pass through.
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
