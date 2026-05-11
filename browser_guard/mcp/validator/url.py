from urllib.parse import urlparse, urlunparse

from .errors import ValidationError


def validate_url(url: str) -> str:
    """Normalize and validate a navigate-target URL.

    Rules:
      - Bare domain (e.g. "amazon.com") is normalized to https://...
      - Path is allowed (e.g., "amazon.com/orders")
      - Query string is rejected
      - Fragment is rejected
      - Result must have a netloc
    """
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        raise ValidationError(f"Invalid URL (no host): {url}")
    if p.query:
        raise ValidationError("Query parameters are not allowed in navigate()")
    if p.fragment:
        raise ValidationError("URL fragments are not allowed in navigate()")
    return urlunparse(p)
