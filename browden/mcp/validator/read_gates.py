"""URL-level read gates: the one predicate the tools gate a URL on.

:func:`is_url_allowed` is the shared decision — does a gate admit a URL's host and
path? :func:`validate_url` (navigate / write-action host check) raises on it and
returns the normalized URL; :func:`ensure_url_is_in_allowlist` (the DOM-read /
screenshot / reload / list_tabs tools) raises on the *read* policy specifically.
"""
from urllib.parse import urlparse, urlunparse

from ...common.logger import logger
from .allowlist import ActionAllowlist, Allowlist, ReadPolicy
from .errors import ValidationError


def is_url_allowed(gate: "Allowlist | ReadPolicy", url: str) -> bool:
    """True iff ``gate`` admits ``url``'s ``(host, path)``.

    ``gate`` is anything with ``is_allowed(host, path)`` — the read
    :class:`ReadPolicy`, or a single write-action :class:`Allowlist` section. The
    shared URL predicate: :func:`validate_url` and
    :func:`ensure_url_is_in_allowlist` both raise on it, and ``list_tabs`` keeps
    tabs by it (H2).

    A URL with no host (``about:blank``, the new-tab page, ``data:``) is decided by
    ``gate`` like any other — its host is ``""``, so a ``*`` read-any override
    admits it while a host-specific policy does not. Per-scheme handling (exempting
    browser-internal pages, gating ``file:``) is left to scheme-allowlisting, added
    with the http/https scheme blocks.
    """
    p = urlparse(url)
    return gate.is_allowed(p.hostname or "", p.path)


def validate_url(url: str, gate: "Allowlist | ReadPolicy") -> str:
    """Normalize and gate a navigate/write-target URL against ``gate``.

    Prepends ``https://`` to a bare host, requires a host (a navigate/write target
    must resolve to one), then requires ``gate`` to admit it (via
    :func:`is_url_allowed`). Raises :class:`ValidationError` on a missing host or a
    blocked one; returns the normalized URL when allowed — query strings and
    fragments pass through unchanged, so '?', '#', '&' and spaces survive.
    """
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        logger.warning(f"URL validation failed: no host in {url!r}")
        raise ValidationError(f"Invalid URL (no host): {url}")
    if not is_url_allowed(gate, url):
        logger.warning(f"URL blocked by allowlist: {p.hostname}{p.path}")
        raise ValidationError(f"URL not on allowlist: {p.hostname}{p.path}")
    logger.info(f"URL allowed: {url!r}")
    return urlunparse(p)


def ensure_url_is_in_allowlist(allowlist: ActionAllowlist, url: str) -> None:
    """Raise :class:`ValidationError` unless the READ policy admits ``url``.

    The read-tool gate (DOM-read / screenshot / reload / list_tabs), the reading
    counterpart of :func:`check_action_host`: checks the tab's live URL so the read
    allowlist governs *reading*, not only navigation — a tab the human (or a
    redirect) parked on a non-allowlisted site is not scrapeable (finding H2)."""
    if not is_url_allowed(allowlist.read_policy, url):
        raise ValidationError(f"URL not on the read allowlist: {urlparse(url).hostname}")
