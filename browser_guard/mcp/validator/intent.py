"""Element-level guard for the ``add_to_cart`` write action.

The per-action host allowlist (:class:`ActionAllowlist`) decides *where*
``add_to_cart`` may act. This module decides *what* it may activate once on an
allowed host: a single, genuine "add to cart" control — never Buy Now, checkout,
subscribe, save-for-later, remove, or an agent-targeted decoy.

Pure: operates on a serialized element node (see
``dom.serialize.element_to_node``), never on Selenium. Real on-screen visibility
and enabled-state are re-verified *live* by the backend at click time; the static
checks here are best-effort defence in depth on the cached snapshot.
"""
import re

# Visible label of a genuine add-to-cart control. Anchored + whitespace-tolerant:
# "Add to Cart", "ADD TO BAG", "Add to basket" match; "Add to cart and check out"
# or "Add to wish list" do not (anchoring) — and the negative list below catches
# anything button-ish that slips through.
_ADD_TO_CART_TEXT = re.compile(r"(?i)^\s*add(ed)?\s+to\s+(cart|bag|basket|trolley)\s*$")

# Contexts that look button-ish but must never be driven by add_to_cart.
_NEGATIVE = re.compile(
    r"(?i)\b("
    r"buy\s*now|buy\s*with|1[\s-]?click|one[\s-]?click|"
    r"checkout|check\s*out|place\s+(your\s+)?order|proceed|"
    r"subscribe|auto[\s-]?deliver|"
    r"remove|delete|save\s+for\s+later|wish\s*list|registry"
    r")\b"
)

# Attributes a page uses to steer AI agents — untrustworthy by construction, so an
# element carrying them is rejected rather than trusted. (See the Amazon page's
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
    """The human-visible names of the control, in trust order. Never raw data-* attrs."""
    attrs = node.get("attributes", {})
    return [
        node.get("text") or "",
        attrs.get("value", ""),
        attrs.get("aria-label", ""),
        attrs.get("title", ""),
    ]


def label_matches(node: dict, pattern: "re.Pattern[str]") -> bool:
    """True iff any human-visible label of ``node`` matches ``pattern``.

    Used to enforce the per-site required button text from the allowlist (e.g.
    Amazon must display "Add to cart"), on top of the generic ``is_add_to_cart``
    check. Matches only trustworthy labels — never raw ``data-*`` attributes.
    """
    if not node:
        return False
    return any(pattern.search(label) for label in _candidate_labels(node))


def is_add_to_cart(node: dict) -> bool:
    """True iff ``node`` is, by trustworthy signals, a genuine add-to-cart control.

    Default-deny: every check must pass. ``node`` is a serialized element dict
    (``{"tag", "id", "classes", "attributes", "text", ...}``) or ``None``.
    """
    if not node:
        return False
    attrs = node.get("attributes", {})

    # 1. Reject agent-targeted decoys outright — never let page-supplied "for AI"
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

    # 4. Never an excluded action (Buy Now / checkout / subscribe / remove / ...).
    haystack = " ".join(_candidate_labels(node) + [str(attrs.get("name", ""))])
    if _NEGATIVE.search(haystack):
        return False

    # 5. Positive identity: at least one human-visible label is exactly "add to cart".
    return any(_ADD_TO_CART_TEXT.search(label) for label in _candidate_labels(node))
