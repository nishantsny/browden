"""Element-level guard for the ``click`` write action.

The per-action host allowlist (:class:`ActionAllowlist`) decides *where* ``click``
may act and, via each host's optional ``label`` regex, *what* text a target may
carry. This module enforces only what the allowlist can't: element *integrity*.
It answers "is this a real, visible, non-decoy clickable control?" — never "is
this the kind of action I approve of." Judging intent (add-to-cart vs. checkout
vs. remove) is the operator's job through the allowlist; a host listed with no
``label`` means every click on it is permitted by design.

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

    # 1. Reject agent-targeted decoys outright — never let tab-supplied "for AI"
    #    markup vouch for an element.
    if any(k in attrs for k in _AGENT_BAIT_KEYS):
        return False

    # 2. Must be a real, statically-plausible clickable control.
    tag = node.get("tag")
    is_button = tag == "button" or attrs.get("role") == "button"
    is_submit = tag == "input" and attrs.get("type", "submit") in _CLICKABLE_INPUT_TYPES
    if not (is_button or is_submit):
        return False

    # 3. Reject anything statically hidden / disabled (live check re-verifies).
    if attrs.get("type") == "hidden" or "hidden" in attrs or attrs.get("aria-hidden") == "true":
        return False
    if "disabled" in attrs or attrs.get("aria-disabled") == "true":
        return False
    if "display:none" in str(attrs.get("style", "")).replace(" ", "").lower():
        return False

    return True
