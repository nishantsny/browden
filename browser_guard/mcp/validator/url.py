from urllib.parse import urlparse, urlunparse

from .allowlist import is_allowed
from .errors import ValidationError


def validate_url(url: str) -> str:
    """Normalize and gate a navigate-target URL against the per-host allowlist.

    A URL is accepted iff its (host, path) matches an entry in allowlist.json.
    For allowed URLs, query strings and fragments are preserved unchanged —
    so '?', '#', '&' and spaces all pass through.
    """
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        raise ValidationError(f"Invalid URL (no host): {url}")
    if not is_allowed(p.hostname or "", p.path):
        raise ValidationError(f"URL not on allowlist: {p.hostname}{p.path}")
    return urlunparse(p)
