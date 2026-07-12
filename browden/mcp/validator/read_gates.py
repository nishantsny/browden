"""URL-level gates for navigation and reading.

:func:`validate_url` gates a navigate/write *target* (it must resolve to a host,
then be admitted by the gate) and returns the normalized URL.
:func:`ensure_url_allowed` reports, as a bool, whether the read policy admits a
tab's live URL (the DOM-read / screenshot / reload / list_tabs tools): it runs the
URL through :func:`validate_url` and turns the raise into ``False``. A
not-yet-navigated tab (``about:blank`` or the browser new-tab page) is always
admitted (special-cased in :func:`validate_url`).
"""
from urllib.parse import urlparse, urlunparse

from ...common.logger import logger
from .allowlist import ActionAllowlist, Allowlist, ReadPolicy
from .errors import ValidationError

# Browser-internal "blank" / new-tab URLs a not-yet-navigated tab reports. Always
# allowed: there is no site to gate, and the agent can't navigate to one
# (validate_url refuses a hostless URL). Matched EXACTLY — deliberately not a
# "chrome://" prefix, which would wave through chrome://settings, downloads, etc.
_ALWAYS_ALLOWED = frozenset({
    "about:blank",
    "chrome://newtab/",
    "chrome://new-tab-page/",
})


def validate_url(url: str, gate: "Allowlist | ReadPolicy") -> str:
    """Normalize and gate a navigate/write-target URL against ``gate``.

    A not-yet-navigated tab's URL (``about:blank`` or the browser new-tab page,
    ``chrome://new-tab-page/``) is special-cased first and returned unchanged: there
    is no site to gate, and prepending ``https://`` would mangle it into a bogus
    host. These are matched exactly (see ``_ALWAYS_ALLOWED``); other non-web schemes
    are left to the PRs that add their handling.

    Otherwise: prepends ``https://`` to a bare host, requires a host (a navigate/
    write target must resolve to one), then requires ``gate`` (anything with
    ``is_allowed(host, path)`` — the read :class:`ReadPolicy` or a write-action
    :class:`Allowlist` section) to admit it. Raises :class:`ValidationError` on a
    missing host or a blocked one; returns the normalized URL when allowed — query
    strings and fragments pass through unchanged, so '?', '#', '&' and spaces
    survive.
    """
    if url in _ALWAYS_ALLOWED:
        return url
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


def ensure_url_allowed(allowlist: ActionAllowlist, url: str) -> bool:
    """Return whether the READ policy admits ``url`` (never raises).

    The read counterpart of :func:`check_action_host`: it runs a tab's live URL
    through :func:`validate_url` against the read policy and reports the outcome as
    a bool, so the read allowlist governs *reading*, not only navigation — a tab the
    human (or a redirect) parked on a non-allowlisted site is not scrapeable
    (finding H2). The read tools raise on a ``False``; ``list_tabs`` closes the tab.
    ``about:blank`` always returns ``True`` (``validate_url`` special-cases it).
    """
    try:
        validate_url(url, allowlist.read_policy)
        return True
    except ValidationError:
        return False
