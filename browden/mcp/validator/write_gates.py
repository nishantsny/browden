"""The full default-deny gate sequences for the write actions (``click`` / ``insert_text``).

Composes the three lower-level pieces — the per-action host allowlist
(:class:`ActionAllowlist`), the URL gate (:func:`validate_url`), and the pure
element-integrity predicates (:mod:`.intent`) — into the exact ordered checks
each write tool must pass before it is allowed to touch the page.

This keeps the server tool thin: the tool does only the I/O (fetch the tab's live
URL, query the element) and hands the results here; every *access decision* lives
in this module, next to the predicates it builds on. Each function raises
:class:`ValidationError` on the first failing gate and returns ``None`` when the
action is authorized.
"""
from urllib.parse import urlparse

from .allowlist import ActionAllowlist
from .errors import ValidationError
from .intent import (
    classify_anchor_target,
    field_id_matches,
    field_label_matches,
    is_clickable_control,
    is_fillable_control,
    label_matches,
)
from .read_gates import validate_url


def check_action_host(allowlist: ActionAllowlist, action: str, url: str) -> None:
    """Gate 1 for a write action: denylist veto, then the action's host allowlist.

    The denylist is consulted first — a denied host is never actionable, even
    when the human opened it — and then the ``action`` section's own host
    allowlist must admit the tab's live ``url``. Raises :class:`ValidationError`
    if either refuses; the caller runs this *before* inspecting the element, so a
    denied host is never even queried.
    """
    parsed = urlparse(url)
    if allowlist.is_denied(parsed.hostname or "", parsed.path):
        raise ValidationError(f"URL on denylist: {parsed.hostname}{parsed.path}")
    validate_url(url, allowlist.section(action))  # raises if host not allowed


def _single_node(found: dict, css_selector: str, refusal: str) -> dict:
    """The one matched element, or a :class:`ValidationError` if zero or many matched.

    ``found`` is a ``query_selector_all`` result fetched with ``limit=2`` so
    "more than one" is detectable without listing them all. ``refusal`` tails the
    ambiguity message (e.g. ``"refusing to click"``).
    """
    total = found["total_count"]
    if total == 0:
        raise ValidationError(f"no element matches selector {css_selector!r}")
    if total > 1:
        raise ValidationError(
            f"selector {css_selector!r} is ambiguous ({total} matches) — {refusal}")
    return found["elements"][0]


def validate_click_target(allowlist: ActionAllowlist, url: str,
                          css_selector: str, found: dict) -> None:
    """Gates 2, 2b and 3 for ``click`` — element integrity, anchor target, label.

    ``url`` is the tab's live URL; ``found`` is the ``query_selector_all(limit=2)``
    result for ``css_selector``. Raises :class:`ValidationError` on the first
    failing gate; returns ``None`` when the click is authorized.
    """
    # Gate 2: the element must be a single, real, visible, non-decoy control.
    node = _single_node(found, css_selector, "refusing to click")
    if not is_clickable_control(node):
        raise ValidationError(
            "selected element is not a clickable control (or is a hidden/disabled/decoy element) — refusing to click")

    # Gate 2b: an <a> anchor may navigate, so gate *where it goes* through the
    # same read allowlist that governs `navigate` — a click that leaves for
    # another site is only as safe as navigating there directly. In-page and
    # javascript: hrefs stay put (no check); an http(s) target must be on the read
    # allowlist (cross-domain is fine if allow-listed); other schemes are refused.
    kind, target = classify_anchor_target(node, url)
    if kind == "blocked":
        raise ValidationError(
            "anchor uses a non-navigational scheme (mailto:/tel:/data:/…) — refusing to click")
    if kind == "nav":
        t = urlparse(target)
        if not allowlist.read_policy.is_allowed(t.hostname or "", t.path):
            raise ValidationError(
                f"anchor target {t.hostname or target!r} is not on the read allowlist — refusing to click")

    # Gate 3: the host's required label. Every listed host has one (config
    # parsing enforces it); '.*' is how a host opts into any control. Fail closed
    # if it is somehow absent rather than waving the click through.
    host = urlparse(url).hostname or ""
    label_re = allowlist.label_pattern("click", host)
    if label_re is None or not label_matches(node, label_re):
        raise ValidationError(
            f"control text does not match the required label for {host} — refusing to click")


def validate_write_text_target(allowlist: ActionAllowlist, url: str,
                               css_selector: str, found: dict) -> None:
    """Gates 2 and 3 for ``insert_text`` — text-control integrity, then label/id.

    ``url`` is the tab's live URL; ``found`` is the ``query_selector_all(limit=2)``
    result for ``css_selector``. Raises :class:`ValidationError` on the first
    failing gate; returns ``None`` when the text entry is authorized.
    """
    # Gate 2: the element must be a single, real, visible, non-decoy text box.
    node = _single_node(found, css_selector, "refusing to insert text")
    if not is_fillable_control(node):
        raise ValidationError(
            "selected element is not a fillable text control (or is a "
            "hidden/disabled/readonly/decoy element) — refusing to insert text")

    # Gate 3: authorize the field either by its visible label (the host's required
    # write-text label regex) OR — for a label-less box the operator has named
    # explicitly — by its exact id/name in the host's field_ids. Fail closed: with
    # no label configured and no id match, nothing is typed.
    host = urlparse(url).hostname or ""
    label_re = allowlist.label_pattern("write-text", host)
    label_ok = label_re is not None and field_label_matches(node, label_re)
    id_ok = field_id_matches(node, allowlist.field_ids("write-text", host))
    if not (label_ok or id_ok):
        raise ValidationError(
            f"field label does not match the required write-text label for {host} — refusing to insert text")
