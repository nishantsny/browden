"""The full default-deny gate sequences for the write actions (``click`` / ``insert_text`` / ``press_key`` / ``upload_file``).

Composes the three lower-level pieces — the per-action host allowlist (in the
:class:`BrowdenAccessRuleSet` each gate is handed), the URL gate
(:func:`validate_url`), and the pure element-integrity predicates
(:mod:`.intent`) — into the exact ordered checks each write tool must pass
before it is allowed to touch the page.

Every *access decision* lives in this module, next to the predicates it builds
on. Each gate function raises :class:`ValidationError` on the first failing gate
and returns ``None`` when the action is authorized.

The tool does not run these itself. It binds them into a :class:`WriteGate`
(``click_gate`` / ``write_text_gate`` / ``press_key_gate``) and hands that to the
session, which runs it inside the same driver-lock hold that performs the action
— see docs/design/gate-atomicity.md for why.
"""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .access_rule_set import BrowdenAccessRuleSet
from .errors import ValidationError
from .intent import (
    ACTIVATION_KEYS,
    classify_anchor_target,
    field_id_matches,
    field_label_matches,
    is_clickable_control,
    is_fillable_control,
    is_focusable_control,
    is_uploadable_control,
    label_matches,
)

# The largest file ``upload-file`` will hand to a page. Not a config knob: it is
# a sanity bound on an action whose real authorization is the location gate,
# and a receipt or a scanned document is orders of magnitude smaller. A workflow
# that needs to ship something bigger than this wants a different tool.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def check_action_host(access_rules: BrowdenAccessRuleSet, action: str, url: str) -> None:
    """Gate 1 for a write action: denylist veto, then the action's host allowlist.

    The denylist is consulted first — a denied host is never actionable, even
    when the human opened it — and then the ``action`` section's own host
    allowlist must admit the tab's live ``url``. Raises :class:`ValidationError`
    if either refuses; the caller runs this *before* inspecting the element, so a
    denied host is never even queried.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if access_rules.is_denied(host, parsed.path):
        raise ValidationError(f"URL on denylist: {parsed.hostname}{parsed.path}")
    # Page-scoped: the host must have at least one rule whose page selector
    # matches this exact URL, or the action is refused here — even on a host that
    # authorizes the action on *other* pages.
    if not access_rules.rules_for(action, host, parsed.path, parsed.query, parsed.fragment):
        raise ValidationError(
            f"no {action} rule authorizes {host}{parsed.path or '/'} — "
            f"{action} not allowed on this page")


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


def validate_click_target(access_rules: BrowdenAccessRuleSet, url: str,
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
        if not access_rules.read_policy.is_allowed(t.hostname or "", t.path, t.query, t.fragment):
            raise ValidationError(
                f"anchor target {t.hostname or target!r} is not on the read allowlist — refusing to click")

    # Gate 3: the label required for THIS page. A control is authorized when some
    # page rule matching this URL has a label the control's text fully matches
    # ('.*' opts a page into any control). rules_for already excludes non-matching
    # pages, so a label allowed elsewhere on the host does not leak onto this one.
    p = urlparse(url)
    rules = access_rules.rules_for("click", p.hostname or "", p.path, p.query, p.fragment)
    if not any(r.label is not None and label_matches(node, r.label) for r in rules):
        raise ValidationError(
            f"control text does not match any click label configured for "
            f"{p.hostname or ''}{p.path or '/'} — refusing to click")


def validate_write_text_target(access_rules: BrowdenAccessRuleSet, url: str,
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

    # Gate 3: authorize the field against the page rules matching THIS URL —
    # either by its visible label (a rule's write-text label regex) OR, for a
    # label-less box the operator named explicitly, by its exact id/name in that
    # same rule's field_ids. Fail closed: no matching rule => nothing is typed, so
    # a field authorized on another page of the host does not leak onto this one.
    p = urlparse(url)
    rules = access_rules.rules_for("write-text", p.hostname or "", p.path, p.query, p.fragment)
    label_ok = any(r.label is not None and field_label_matches(node, r.label) for r in rules)
    id_ok = any(field_id_matches(node, r.field_ids) for r in rules)
    if not (label_ok or id_ok):
        raise ValidationError(
            f"field label does not match any write-text rule for "
            f"{p.hostname or ''}{p.path or '/'} — refusing to insert text")


def validate_press_key_target(access_rules: BrowdenAccessRuleSet, url: str,
                              css_selector: str, found: dict, key: str) -> None:
    """Gates 2, 2b and 3 for ``press-key`` — focusability, control-key, then label+key.

    ``url`` is the tab's live URL; ``found`` is the ``query_selector_all(limit=2)``
    result for ``css_selector``; ``key`` is the W3C ``key`` value the caller wants
    to send. Raises :class:`ValidationError` on the first failing gate; returns
    ``None`` when the key press is authorized. Gate 1 (host + page section) is
    :func:`check_action_host`, run first by the :class:`WriteGate`, exactly as for ``click``.
    """
    # Gate 2: the element must be a single, real, visible, non-decoy *focusable*
    # control (natively focusable or tabindex) — the keyboard analogue of the
    # is_clickable_control integrity check.
    node = _single_node(found, css_selector, "refusing to press a key")
    if not is_focusable_control(node):
        raise ValidationError(
            "selected element is not a focusable control (or is a "
            "hidden/disabled/decoy element) — refusing to press a key")

    # Gate 2b: only control keys ever go through press-key. Character keys are
    # refused outright — typing text is write-text's job (gated by field label);
    # letting characters through here would be a text-entry channel that skips it.
    if key not in ACTIVATION_KEYS:
        raise ValidationError(
            f"key {key!r} is not an allowed control key — press-key sends only "
            f"{sorted(ACTIVATION_KEYS)}; type text with insert_text instead")

    # Gate 3: some page rule matching THIS url must both admit the control (its
    # visible-text label matches) AND list this key. Authority is per page and per
    # key — a rule that allows Enter on the picker doesn't thereby allow Escape,
    # and one that allows a control here does not leak onto another page.
    p = urlparse(url)
    rules = access_rules.rules_for("press-key", p.hostname or "", p.path, p.query, p.fragment)
    if not any(r.label is not None and label_matches(node, r.label) and key in r.keys
               for r in rules):
        raise ValidationError(
            f"no press-key rule authorizes key {key!r} on this control for "
            f"{p.hostname or ''}{p.path or '/'} — refusing to press a key")


def resolve_upload_path(file_path: str) -> Path:
    """The caller's path as one real, absolute path — expanded and fully resolved.

    The single transform between what the agent asks to upload and what is judged
    and then sent, so the file the gate admitted is the file the browser opens.
    Resolving the whole chain (``~``, ``..`` segments, every symlink) is what lets
    the containment check below be a simple one.
    """
    return Path(file_path).expanduser().resolve()


def validate_upload_path(allowed_upload_locations: "tuple[Path, ...]", file_path: str) -> Path:
    """Gate 4 for ``upload-file``: the file must be a real file in an allowed location.

    The gate with no analogue in the other write actions, and the reason this is
    an action of its own. ``click`` and ``insert_text`` act with data the agent
    already has; an upload makes browden **read the local filesystem and ship the
    bytes to a website**. With nowhere declared allowed, "upload to host X" means
    "exfiltrate ``~/.ssh/id_rsa`` to host X", and the tool is an arbitrary
    local-file read primitive wearing a form control.

    So: the path is expanded and **fully resolved**, and the result must sit under
    one of the operator's resolved ``allowed_upload_locations``. Resolving first
    is what closes the two ways out of an allowed location — ``../`` traversal in
    the path the agent passes, and a symlink *inside* one pointing anywhere on
    disk. The file must also exist, be a regular file (not a directory, FIFO or
    device) and be under :data:`MAX_UPLOAD_BYTES`.

    No location configured means no upload is authorized — including under
    ``allow_all``, which grants authority over *pages* and says nothing about the
    filesystem. Returns the resolved path to hand the backend; raises
    :class:`ValidationError` otherwise.
    """
    if not allowed_upload_locations:
        raise ValidationError(
            "no allowed_upload_locations are configured — upload-file is not "
            "authorized to read any local file (add an allowed_upload_locations "
            "list to the allowlist)")
    try:
        resolved = resolve_upload_path(file_path)
    except (OSError, ValueError, RuntimeError) as e:
        raise ValidationError(f"{file_path!r} is not a usable path: {e}") from None
    if not any(resolved == allowed or resolved.is_relative_to(allowed)
               for allowed in allowed_upload_locations):
        raise ValidationError(
            f"{str(resolved)!r} is outside every allowed upload location "
            f"({', '.join(str(r) for r in allowed_upload_locations)}) — refusing to upload")
    try:
        if not resolved.is_file():
            raise ValidationError(
                f"{str(resolved)!r} is not an existing regular file — refusing to upload")
        size = resolved.stat().st_size
    except OSError as e:
        raise ValidationError(f"cannot read {str(resolved)!r}: {e}") from None
    if size > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"{str(resolved)!r} is {size} bytes, over the {MAX_UPLOAD_BYTES}-byte "
            f"upload cap — refusing to upload")
    return resolved


def validate_upload_target(access_rules: BrowdenAccessRuleSet, url: str,
                           css_selector: str, found: dict) -> None:
    """Gates 2 and 3 for ``upload-file`` — file-control integrity, then label/id.

    ``url`` is the tab's live URL; ``found`` is the ``query_selector_all(limit=2)``
    result for ``css_selector``. Raises :class:`ValidationError` on the first
    failing gate; returns ``None`` when the upload is authorized.

    Gate 3 mirrors ``write-text``'s label-or-id shape because this target needs
    the id half even more often: a file input routinely carries no visible label
    of any kind (Splitwise's receipt picker has a sibling ``<p>``, not a
    ``<label for>``, and no placeholder / aria-label / title), so no ``label``
    regex could ever authorize it.
    """
    # Gate 2: the element must be a single, real, visible, non-decoy file input.
    node = _single_node(found, css_selector, "refusing to upload")
    if not is_uploadable_control(node):
        raise ValidationError(
            "selected element is not a file input (or is a "
            "hidden/disabled/readonly/decoy element) — refusing to upload")

    # Gate 3: authorize the control against the page rules matching THIS URL —
    # by its visible label, or by the exact id/name the operator named in that
    # same rule's field_ids. Fail closed, exactly as write-text does.
    p = urlparse(url)
    rules = access_rules.rules_for("upload-file", p.hostname or "", p.path, p.query, p.fragment)
    label_ok = any(r.label is not None and field_label_matches(node, r.label) for r in rules)
    id_ok = any(field_id_matches(node, r.field_ids) for r in rules)
    if not (label_ok or id_ok):
        raise ValidationError(
            f"file input does not match any upload-file rule for "
            f"{p.hostname or ''}{p.path or '/'} — refusing to upload")


@dataclass(frozen=True)
class WriteGate:
    """The full gate sequence for one write request, bound to one rule set.

    Built once per request, from a single read of the access rules, and run by
    the session *inside the driver-lock hold that performs the action*. So the
    URL it judges, the element it judges, the rules it judges them by and the
    page the action lands on are all the same — nothing (a concurrent
    ``navigate``, a config hot-reload) can come between the decision and the
    action. Both checks raise :class:`ValidationError` to refuse.
    """
    check_page: Callable[[str], None]                # gate 1: (url); runs before the DOM is read
    check_element: Callable[[str, str, dict], None]  # gates 2+: (url, css_selector, found)


def click_gate(access_rules: BrowdenAccessRuleSet) -> WriteGate:
    """The ``click`` gates, bound to ``access_rules``."""
    return WriteGate(
        check_page=lambda url: check_action_host(access_rules, "click", url),
        check_element=lambda url, css_selector, found: validate_click_target(
            access_rules, url, css_selector, found))


def write_text_gate(access_rules: BrowdenAccessRuleSet) -> WriteGate:
    """The ``insert_text`` gates, bound to ``access_rules``."""
    return WriteGate(
        check_page=lambda url: check_action_host(access_rules, "write-text", url),
        check_element=lambda url, css_selector, found: validate_write_text_target(
            access_rules, url, css_selector, found))


def press_key_gate(access_rules: BrowdenAccessRuleSet, key: str) -> WriteGate:
    """The ``press_key`` gates for ``key``, bound to ``access_rules``."""
    return WriteGate(
        check_page=lambda url: check_action_host(access_rules, "press-key", url),
        check_element=lambda url, css_selector, found: validate_press_key_target(
            access_rules, url, css_selector, found, key))


def upload_file_gate(access_rules: BrowdenAccessRuleSet, file_path: str) -> WriteGate:
    """The ``upload_file`` gates for ``file_path``, bound to ``access_rules``.

    The filesystem gate runs in ``check_page``, i.e. **before the DOM is read**:
    there is no reason to inspect a page for an upload that is refused whatever
    the page holds, and it keeps the decision inside the one driver hold with
    every other gate (see docs/design/gate-atomicity.md).
    """
    def check_page(url: str) -> None:
        check_action_host(access_rules, "upload-file", url)
        validate_upload_path(access_rules.allowed_upload_locations, file_path)

    return WriteGate(
        check_page=check_page,
        check_element=lambda url, css_selector, found: validate_upload_target(
            access_rules, url, css_selector, found))
