"""A URL's web origin — the browser's own same-origin boundary."""
from urllib.parse import urlparse

_DEFAULT_PORTS = {"http": 80, "https": 443}


def url_origin(url: str) -> tuple[str, str, int | None] | None:
    """``(scheme, host, port)`` for ``url``, or ``None`` if it has no origin of its own.

    Exact, as the browser compares origins: the scheme, the lower-cased host and
    the port (a default port made explicit, so ``https://a`` and ``https://a:443``
    are one origin). No other normalization — ``www.a`` and ``a``, or ``http`` and
    ``https``, are different origins. ``about:``, ``data:`` and other opaque URLs,
    and a malformed port, have no origin: ``None``, which matches nothing.
    """
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return None
    if not p.scheme or not p.hostname:
        return None
    scheme = p.scheme.lower()
    return scheme, p.hostname.lower(), port if port is not None else _DEFAULT_PORTS.get(scheme)


def same_origin(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` share one origin. Never true for an opaque URL."""
    origin = url_origin(a)
    return origin is not None and origin == url_origin(b)
