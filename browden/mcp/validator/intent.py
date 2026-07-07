"""Element-level guard for the write actions (``click`` and ``insert_text``).

The per-action host allowlist (:class:`ActionAllowlist`) decides *where* an action
may act and, via each host's optional ``label`` regex, *what* target may carry.
This module enforces only what the allowlist can't: element *integrity*. It
answers "is this a real, visible, non-decoy control (a clickable one for
``click``, a text box for ``insert_text``)?" — never "is this the kind of action I
approve of." Judging intent (add-to-cart vs. checkout vs. remove; which fields may
be typed into) is the operator's job through the allowlist; a host listed with no
``label`` means every action on it is permitted by design.

Pure: operates on a serialized element node (see
``dom.serialize.element_to_node``), never on Selenium. Real on-screen visibility
and enabled-state are re-verified *live* by the backend at click time; the static
checks here are best-effort defence in depth on the cached snapshot.
"""

from urllib.parse import urljoin, urlparse

# Attributes a tab uses to steer AI agents — untrustworthy by construction, so an
# element carrying them is rejected rather than trusted. (See the Amazon tab's
# decoy controls: data-target-audience="ai-agent", data-agent-recommended, ...)
_AGENT_BAIT_KEYS = (
    "data-agent-action",
    "data-agent-recommended",
    "data-agent-priority",
    "data-agent-button-proxy",
    "data-target-audience",
)

_CLICKABLE_INPUT_TYPES = ("submit", "button")

# <input> types that hold free text the user types into (the `insert_text` action). A
# bare <input> with no type defaults to "text", so it counts too. Deliberately
# excludes non-text inputs (checkbox/radio/file/range/color/date-pickers/etc.) —
# those are manipulated by clicking, not typing.
_TEXT_INPUT_TYPES = ("text", "search", "email", "tel", "url", "number", "password")


def _fails_integrity(attrs: dict) -> bool:
    """Shared anti-injection + statically-hidden/disabled rejection for any write target.

    Rejects (a) agent-targeted decoys — never let tab-supplied "for AI" markup
    vouch for an element — and (b) elements the snapshot shows as hidden or
    disabled. The backend re-verifies visibility/enabled live at action time; this
    is best-effort defence in depth on the cached node.
    """
    if any(k in attrs for k in _AGENT_BAIT_KEYS):
        return True
    if attrs.get("type") == "hidden" or "hidden" in attrs or attrs.get("aria-hidden") == "true":
        return True
    if "disabled" in attrs or attrs.get("aria-disabled") == "true":
        return True
    if "display:none" in str(attrs.get("style", "")).replace(" ", "").lower():
        return True
    return False


def _candidate_labels(node: dict) -> list[str]:
    """The human-visible names of the control, in trust order. Never raw data-* attrs.

    ``labelledby_text`` is the accessible name the serializer resolved from
    ``aria-labelledby`` — the visible label when it lives on a referenced element
    rather than on the control itself (see ``dom.serialize._labelledby_text``).
    It is the same class of authored label as ``aria-label``, so it belongs here.
    """
    attrs = node.get("attributes", {})
    return [
        node.get("text") or "",
        attrs.get("value", ""),
        attrs.get("aria-label", ""),
        attrs.get("title", ""),
        node.get("labelledby_text") or "",
    ]


def label_matches(node: dict, pattern: "re.Pattern[str]") -> bool:
    """True iff some human-visible label of ``node`` is matched *in full* by ``pattern``.

    Used to enforce the per-site required button text from the allowlist (e.g.
    Amazon must display "Add to cart"), on top of the generic
    ``is_clickable_control`` integrity check. Matches only trustworthy labels —
    never raw ``data-*`` attributes.

    The pattern must match the **entire** label (``fullmatch``), not merely a
    substring of it: the operator's regex fully governs what counts, so a loose
    pattern can't accidentally pass a control whose visible text is
    ``"Add to cart and check out"``. A label with incidental surrounding
    whitespace or a trailing item name still needs that spelled out in the regex
    (e.g. ``(?i)\\s*add to cart\\s*`` or ``(?i)add to cart\\b.*``).
    """
    if not node:
        return False
    return any(pattern.fullmatch(label) for label in _candidate_labels(node))


def is_clickable_control(node: dict) -> bool:
    """True iff ``node`` is a real, visible, non-decoy clickable control.

    Integrity + anti-injection only — this does **not** judge what the control
    *does*. Whether a Buy Now, checkout, or remove button may be clicked is the
    operator's decision, expressed per host in the allowlist ``label`` (an absent
    label permits any). This guard exists to stop the *page* from tricking the
    agent, not to second-guess the operator.

    Accepts ``<button>``, ``role="button"``, ``<input type=submit|button>``, and
    ``<a>`` anchors. Anchors *navigate*, so they carry one extra obligation the
    others don't: their href must resolve to a site the read allowlist permits —
    that is decided separately via :func:`classify_anchor_target` (which needs the
    current URL and the read policy, neither available here), never by this pure
    integrity check.

    Default-deny: every check must pass. ``node`` is a serialized element dict
    (``{"tag", "id", "classes", "attributes", "text", ...}``) or ``None``.
    """
    if not node:
        return False
    attrs = node.get("attributes", {})

    # 1. Reject agent decoys + statically hidden/disabled elements.
    if _fails_integrity(attrs):
        return False

    # 2. Must be a real, statically-plausible clickable control.
    tag = node.get("tag")
    is_button = tag == "button" or attrs.get("role") == "button"
    is_submit = tag == "input" and attrs.get("type", "submit") in _CLICKABLE_INPUT_TYPES
    is_anchor = tag == "a"
    return is_button or is_submit or is_anchor


def classify_anchor_target(node: dict, current_url: str) -> "tuple[str, str | None]":
    """Classify what clicking anchor ``node`` would navigate to, for the click gate.

    An anchor is the one clickable control that can whisk the agent to another
    site, so — rather than trusting it stays on the current domain — the caller
    gates *where it goes* through the same read allowlist that governs
    ``navigate``. This returns ``(kind, target)`` telling the caller how:

    * ``("inpage", None)`` — no navigation to a fetchable page: ``node`` isn't an
      ``<a>``, has no href, or the href is empty / a ``javascript:`` handler that
      runs in place (e.g. Amazon's tip "Edit"). No allowlist check needed; the
      page you are already on was already allowed.
    * ``("nav", url)`` — an ``http(s)`` navigation to absolute ``url`` (the href
      resolved against ``current_url``; a relative or ``#fragment`` href resolves
      back onto the current site). The caller MUST gate ``url`` through the read
      allowlist before allowing the click — it is only as safe as ``navigate`` to
      that same URL. Note this is a *target-in-allowlist* test, not a same-domain
      one: a cross-domain link to an allow-listed site is fine, and a same-site
      link to a path the read policy denies is not.
    * ``("blocked", None)`` — a scheme that leaves or repurposes the browsing
      context (``mailto:``, ``tel:``, ``data:``, ``file:``, …), which the read
      allowlist doesn't reason about; the caller should refuse.

    Best-effort: it governs the *declarative* href only. A page's JS can still
    navigate anywhere after any click (button or anchor alike); that is out of
    scope here just as it is for buttons.
    """
    if not node or node.get("tag") != "a":
        return ("inpage", None)
    href = (node.get("attributes", {}).get("href") or "").strip()
    if not href:
        return ("inpage", None)
    resolved = urljoin(current_url, href)
    scheme = urlparse(resolved).scheme.lower()
    if scheme in ("", "javascript"):
        return ("inpage", None)  # in-page fragment or a JS onclick handler
    if scheme in ("http", "https"):
        return ("nav", resolved)
    return ("blocked", None)  # mailto:, tel:, data:, file:, …


# -- insert_text (write-text) side --------------------------------------------------

def is_fillable_control(node: dict) -> bool:
    """True iff ``node`` is a real, visible, non-decoy **text-entry** control.

    The ``insert_text`` analogue of :func:`is_clickable_control`: integrity + anti-decoy
    only, for the *write-text* action. It says "is this a text box a human could
    type into" — a ``<textarea>``, a text-like ``<input>`` (see
    ``_TEXT_INPUT_TYPES``), or a ``contenteditable`` element — and rejects decoys,
    hidden/disabled elements, and read-only fields. *Which* fields may be typed
    into, and *what* value they may receive, is the operator's decision via the
    ``write-text`` allowlist label (see :func:`field_label_matches`).

    Default-deny: every check must pass. ``node`` is a serialized element dict.
    """
    if not node:
        return False
    attrs = node.get("attributes", {})
    if _fails_integrity(attrs):
        return False
    # A field the user could not type into is not fillable.
    if "readonly" in attrs or attrs.get("aria-readonly") == "true":
        return False

    tag = node.get("tag")
    if tag == "textarea":
        return True
    if tag == "input" and attrs.get("type", "text") in _TEXT_INPUT_TYPES:
        return True
    # contenteditable="" / "true" / "plaintext-only" makes any element editable;
    # "false" (or absent) does not.
    ce = attrs.get("contenteditable")
    return ce is not None and ce.lower() in ("", "true", "plaintext-only")


def _field_labels(node: dict) -> list[str]:
    """The human-visible names of a text field, for the write-text gate.

    A text box usually carries no visible text of its own, so we match the
    operator's ``write-text`` regex against what a human reads as the field's
    name: its ``placeholder``, ``aria-label``, the ``aria-labelledby`` /
    ``<label>`` text the serializer resolved (``labelledby_text`` / ``field_label``,
    see ``dom.serialize``), and ``title``. Never raw ``name``/``id``/``data-*`` —
    those aren't visible to the user.
    """
    attrs = node.get("attributes", {})
    return [
        attrs.get("placeholder", ""),
        attrs.get("aria-label", ""),
        node.get("labelledby_text") or "",
        node.get("field_label") or "",
        attrs.get("title", ""),
    ]


def field_label_matches(node: dict, pattern: "re.Pattern[str]") -> bool:
    """True iff some visible label of the text field is matched *in full* by ``pattern``.

    The write-text counterpart of :func:`label_matches`: the operator's
    ``write-text`` label regex must ``fullmatch`` the field's visible name (from
    :func:`_field_labels`), so an operator authorizes *which* boxes may be typed
    into by the label a human sees next to them — never by hidden identifiers.
    """
    if not node:
        return False
    return any(pattern.fullmatch(label) for label in _field_labels(node))


def field_id_matches(node: dict, allowed_ids: "set[str]") -> bool:
    """True iff the field's own ``id`` or ``name`` attribute is in ``allowed_ids``.

    The deliberate escape hatch for text boxes that carry **no visible label** at
    all — e.g. Amazon Fresh's grocery-tip ``<input>``, which has no placeholder,
    ``aria-label``, ``aria-labelledby``, or associated ``<label>``, so
    :func:`field_label_matches` can never authorize it. Here the operator instead
    names the field by its stable ``id``/``name`` in the write-text ``field_ids``
    list.

    Unlike the label path this trusts a **non-visible** identifier, so it is
    strictly opt-in per field and never a default: ``allowed_ids`` is empty for
    every host that doesn't list ``field_ids``, and an empty set matches nothing.
    A page could in principle put a listed ``id`` on a different field, but only a
    field on a host the operator already allow-listed for write-text — the
    identifier is a name the operator chose, not one the page volunteered.
    """
    if not node or not allowed_ids:
        return False
    attrs = node.get("attributes", {})
    return node.get("id") in allowed_ids or attrs.get("name") in allowed_ids
