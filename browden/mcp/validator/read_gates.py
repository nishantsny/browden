"""URL-level gates for navigation and reading.

:func:`validate_url` gates a navigate/write *target* (it must resolve to a host,
then be admitted by the gate) and returns the normalized URL.
:func:`ensure_url_allowed` gates a *read* of a tab's live URL (the DOM-read /
screenshot / reload / list_tabs tools) against the read policy, always allowing a
not-yet-navigated ``about:blank`` tab. Both raise :class:`ValidationError` when
the URL is not allowed.
"""
from urllib.parse import urlparse, urlunparse

from ...common.logger import logger
from .allowlist import ActionAllowlist, Allowlist, ReadPolicy
from .errors import ValidationError

# Browser-internal URLs a not-yet-navigated tab reports; always readable — there
# is no site to gate, and the agent can't navigate to one (validate_url refuses a
# hostless URL). Gating other non-web schemes (file:, …) is scheme-allowlisting,
# added later with the http/https scheme blocks.
_ALWAYS_READABLE = frozenset({"about:blank"})


def validate_url(url: str, gate: "Allowlist | ReadPolicy") -> str:
    """Normalize and gate a navigate/write-target URL against ``gate``.

    Prepends ``https://`` to a bare host, requires a host (a navigate/write target
    must resolve to one), then requires ``gate`` (anything with
    ``is_allowed(host, path)`` — the read :class:`ReadPolicy` or a write-action
    :class:`Allowlist` section) to admit it. Raises :class:`ValidationError` on a
    missing host or a blocked one; returns the normalized URL when allowed — query
    strings and fragments pass through unchanged, so '?', '#', '&' and spaces
    survive.
    """
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        logger.warning(f"URL validation failed: no host in {url!r}")
        raise ValidationError(f"Invalid URL (no host): {url}")
    if not gate.is_allowed(p.hostname or "", p.path):
        logger.warning(f"URL blocked by allowlist: {p.hostname}{p.path}")
        raise ValidationError(f"URL not on allowlist: {p.hostname}{p.path}")
    logger.info(f"URL allowed: {url!r}")
    return urlunparse(p)


def ensure_url_allowed(allowlist: ActionAllowlist, url: str) -> None:
    """Raise :class:`ValidationError` unless the READ policy admits ``url``.

    The read-tool gate (DOM-read / screenshot / reload / list_tabs), the reading
    counterpart of :func:`check_action_host`: it checks the tab's live URL so the
    read allowlist governs *reading*, not only navigation — a tab the human (or a
    redirect) parked on a non-allowlisted site is not scrapeable (finding H2). A
    not-yet-navigated ``about:blank`` tab is always readable (nothing to gate).
    """
    if url in _ALWAYS_READABLE:
        return
    p = urlparse(url)
    if not allowlist.read_policy.is_allowed(p.hostname or "", p.path):
        raise ValidationError(f"URL not on the read allowlist: {p.hostname}")
