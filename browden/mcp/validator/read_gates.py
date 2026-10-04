"""URL-level gates for navigation and reading.

:func:`validate_url` gates a navigate/write *target* (it must resolve to a host,
then be admitted by the gate) and returns the normalized URL.
:func:`ensure_url_allowed` reports, as a bool, whether the read policy admits a
tab's live URL (the DOM-read / screenshot / reload / list_tabs tools): it runs the
URL through :func:`validate_url` and turns the raise into ``False``. A
not-yet-navigated tab (``about:blank`` or the browser new-tab page) is always
admitted (special-cased in :func:`validate_url`).
"""
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from ...common.logger import logger
from ...common.origin import same_origin
from .allowlist import HostRuleMatcher, ReadPolicy
from .access_rule_set import BrowdenAccessRuleSet
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


def validate_url(url: str, gate: "HostRuleMatcher | ReadPolicy") -> str:
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
    :class:`HostRuleMatcher`) keep their prior scheme-agnostic behavior.

    Otherwise: prepends ``https://`` to a bare host, requires a host (a navigate/
    write target must resolve to one — except ``file://``, which is authority-less
    and gated on its path), then requires ``gate`` (anything with
    ``is_allowed(host, path)`` — the read :class:`ReadPolicy` or a write-action
    :class:`HostRuleMatcher` section) to admit it. Raises :class:`ValidationError` on a
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
    # re-open file:// or plaintext http everywhere. A write-action HostRuleMatcher has
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
    if not gate.is_allowed(host, p.path, p.query, p.fragment):
        logger.warning(f"URL blocked by allowlist: {p.hostname}{p.path}")
        raise ValidationError(f"URL not on allowlist: {p.hostname}{p.path}")
    logger.info(f"URL allowed: {url!r}")
    return urlunparse(p)


def ensure_url_allowed(access_rules: BrowdenAccessRuleSet, url: str) -> bool:
    """Return whether the READ policy admits ``url`` (never raises).

    The read counterpart of :func:`check_action_host`: it runs a tab's live URL
    through :func:`validate_url` against the read policy and reports the outcome as
    a bool, so the read allowlist governs *reading*, not only navigation — a tab the
    human (or a redirect) parked on a non-allowlisted site is not scrapeable
    (finding H2). The read tools raise on a ``False``; ``list_tabs`` closes the tab.
    ``about:blank`` always returns ``True`` (``validate_url`` special-cases it).
    """
    try:
        validate_url(url, access_rules.read_policy)
        return True
    except ValidationError:
        return False


@dataclass(frozen=True)
class ReadGate:
    """The read gate for one request, bound to one rule set.

    Built once per request from a single read of the access rules, and run by
    the session *inside the driver-lock hold that reads the page* — on the tab's
    live URL before any content leaves the browser, and again on wherever a
    navigation or reload in that same hold actually landed. So nothing (a
    concurrent redirecting ``navigate``, a config hot-reload) can come between
    the check and the read. ``check_page`` raises :class:`ValidationError` to
    refuse. See docs/design/gate-atomicity.md.
    """
    check_page: Callable[[str], None]


def read_gate(access_rules: BrowdenAccessRuleSet) -> ReadGate:
    """The read gate, bound to ``access_rules``."""
    def check_page(url: str) -> None:
        if not ensure_url_allowed(access_rules, url):
            raise ValidationError(f"URL not on the read allowlist: {url}")
    return ReadGate(check_page=check_page)


def validate_and_ensure_same_origin(
    top_url: str, frame_url: str, gate: "HostRuleMatcher | ReadPolicy",
) -> None:
    """Gate a frame focus has landed on: read-allowed AND same-origin. Raises on refusal.

    Called after a switch into a frame (and on every ascent), with the landed
    document's effective URL and the tab's top-level URL. Both conditions hold:

    1. ``frame_url`` must be admitted by the read policy (``validate_url``) — a frame
       is a distinct document and must itself be readable to be inspected.
    2. ``frame_url`` must have the top page's exact **origin** (scheme, host, port —
       :func:`~browden.common.origin.same_origin`): the browser's own boundary, so
       ``http://shop.example`` inside ``https://www.shop.example`` is refused.

    Same-origin is a deliberate scope limit, not a crutch for the write gates:
    reads and writes inside a frame are judged by the frame's own URL, each under
    its own gate (the read allowlist, the action's host and element rules). A
    frame the page wrote (``srcdoc``) has its parent's URL, so the parent's rules
    apply to it. Admitting cross-origin frames is left for later.

    On a raise the caller restores the previous focus (entry) or retreats to the
    top document (ascent); nothing is read or done inside a refused frame.
    ``top_url`` is trusted here: the caller has already gated it.
    """
    validate_url(frame_url, gate)  # the landed document must itself be read-allowed
    if not same_origin(top_url, frame_url):
        raise ValidationError(
            f"cross-origin frame refused (same-origin only): {frame_url!r} is not "
            f"the same origin as the page {top_url!r}")


@dataclass(frozen=True)
class FrameGate:
    """The frame gates for one ``switch_to_frame`` / ascent request, bound to one rule set.

    Built once per request from a single read of the access rules, and run by the
    session *inside the one driver-lock hold that moves the focus* — so the
    document descended from, the frame's declared target and the document landed
    on are all judged in the hold that switches, by the same rules. All three raise
    :class:`ValidationError` to refuse. See docs/design/gate-atomicity.md.
    """
    check_page: Callable[[str], None]          # (url): the focused document, before descending
    check_src: Callable[[str], None]           # (src): the iframe's declared target, before switching
    check_landed: Callable[[str, str], None]   # (top_url, frame_url): read-allowed + same-origin


def frame_gate(access_rules: BrowdenAccessRuleSet) -> FrameGate:
    """The frame gates, bound to ``access_rules``."""
    read = read_gate(access_rules)
    return FrameGate(
        check_page=read.check_page,
        check_src=lambda src: validate_url(src, access_rules.read_policy),
        check_landed=lambda top_url, frame_url: validate_and_ensure_same_origin(
            top_url, frame_url, access_rules.read_policy))
