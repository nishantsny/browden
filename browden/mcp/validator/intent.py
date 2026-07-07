"""Element-level guard for the write actions (``click`` and ``fill``).

The per-action host allowlist (:class:`ActionAllowlist`) decides *where* an action
may act and, via each host's optional ``label`` regex, *what* target may carry.
This module enforces only what the allowlist can't: element *integrity*. It
answers "is this a real, visible, non-decoy control (a clickable one for
``click``, a text box for ``fill``)?" — never "is this the kind of action I
approve of." Judging intent (add-to-cart vs. checkout vs. remove; which fields may
be typed into) is the operator's job through the allowlist; a host listed with no
``label`` means every action on it is permitted by design.

Pure: operates on a serialized element node (see
``dom.serialize.element_to_node``), never on Selenium. Real on-screen visibility
and enabled-state are re-verified *live* by the backend at click time; the static
checks here are best-effort defence in depth on the cached snapshot.
"""

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

# <input> types that hold free text the user types into (the `fill` action). A
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
    return is_button or is_submit


# -- fill (write-text) side --------------------------------------------------

def is_fillable_control(node: dict) -> bool:
    """True iff ``node`` is a real, visible, non-decoy **text-entry** control.

    The ``fill`` analogue of :func:`is_clickable_control`: integrity + anti-decoy
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
