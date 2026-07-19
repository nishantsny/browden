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
from .tranco import canonical_host

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
    host. These are matched exactly (see ``_ALWAYS_ALLOWED``).

    **Scheme gate (#70 M2).** For the read policy, only ``https`` is accepted by
    default, so ``file://localhost/etc/passwd`` and ``ftp://…`` can't slip through
    on a permissive host rule. A non-https scheme is allowed only for a host the
    operator has *explicitly* overridden (``gate.override_has_host``) — so
    ``localhost: [".*"]`` re-enables ``http://localhost`` and ``"": ["^/x/.*"]``
    re-enables ``file://`` paths, while a blanket ``"*": [".*"]`` does not silently
    re-open non-https everywhere. Gates without that notion (a write-action
    :class:`Allowlist`) keep their prior scheme-agnostic behavior.

    Otherwise: prepends ``https://`` to a bare host, requires a host (a navigate/
    write target must resolve to one — except ``file://``, which is authority-less
    and gated on its path), then requires ``gate`` (anything with
    ``is_allowed(host, path)`` — the read :class:`ReadPolicy` or a write-action
    :class:`Allowlist` section) to admit it. Raises :class:`ValidationError` on a
    disallowed scheme, a missing host, or a blocked ``(host, path)``; returns the
    normalized URL when allowed — query strings and fragments pass through
    unchanged, so '?', '#', '&' and spaces survive.
    """
    if url in _ALWAYS_ALLOWED:
        return url
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    scheme = p.scheme.lower()
    host = p.hostname or ""
    # Scheme gate: only the read policy carries scheme intent (it exposes
    # override_has_host). A non-https scheme is admitted only for a host the
    # operator explicitly overrode, so a blanket "*": [".*"] does not silently
    # re-open file:// or plaintext http everywhere. A write-action Allowlist has
    # no such method and keeps its scheme-agnostic behavior.
    override_has_host = getattr(gate, "override_has_host", None)
    if override_has_host is not None and scheme != "https" and not override_has_host(host):
        logger.warning(f"URL scheme blocked: {scheme!r} in {url!r} "
                       f"(only https, unless the host has an explicit read override)")
        raise ValidationError(
            f"URL scheme not allowed: {scheme!r} — only https, unless {host!r} has an "
            f"explicit website_overrides entry")
    # http(s) et al. must name a host; file:// legitimately has no authority
    # (file:///etc/passwd) and is gated on its path against the allowlist below.
    if scheme != "file" and not p.netloc:
        logger.warning(f"URL validation failed: no host in {url!r}")
        raise ValidationError(f"Invalid URL (no host): {url}")
    if not gate.is_allowed(host, p.path):
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


def validate_and_ensure_same_origin(
    top_url: str, frame_url: str, gate: "Allowlist | ReadPolicy",
) -> None:
    """Gate entering an iframe (v1: same-origin only). Raises on refusal.

    Called AFTER the driver has switched into the frame, with the frame's *actual*
    ``document.URL`` and the tab's top-level URL. Two conditions, both required:

    1. ``frame_url`` must be admitted by the read policy (``validate_url``) — a frame
       is a distinct document and must itself be readable to be inspected.
    2. The frame's host must equal the top page's host (**same-origin**). Cross-origin
       frames are refused in v1 because the click/write host gate keys off the tab's
       top URL (``driver.current_url`` stays top-level inside a frame), so it cannot
       correctly govern a different-origin document — enabling that safely needs a
       frame-aware write gate (a future v2).

    The caller runs this post-switch and, on a raise, returns the driver to the top
    document (no action is taken inside a refused frame). ``top_url`` is trusted here:
    the caller has already gated it via the read policy before switching.
    """
    validate_url(frame_url, gate)  # the landed document must itself be read-allowed
    frame_host = canonical_host(urlparse(frame_url).hostname or "")
    top_host = canonical_host(urlparse(top_url).hostname or "")
    if frame_host != top_host:
        raise ValidationError(
            f"cross-origin frame refused (same-origin only): frame host {frame_host!r} "
            f"!= page host {top_host!r}")
