"""Re-export validator symbols for convenient access."""
from .allowlist import ActionAllowlist, Allowlist, ReadPolicy
from .errors import ValidationError, tab_gone_envelope
from .intent import (
    classify_anchor_target,
    field_id_matches,
    field_label_matches,
    is_clickable_control,
    is_fillable_control,
    label_matches,
)
from .tranco import TrancoList
from .read_gates import ensure_url_allowed, validate_url
from .write_gates import (
    check_action_host,
    validate_click_target,
    validate_write_text_target,
)

__all__ = [
    "ActionAllowlist",
    "Allowlist",
    "ReadPolicy",
    "TrancoList",
    "ValidationError",
    "check_action_host",
    "classify_anchor_target",
    "ensure_url_allowed",
    "field_id_matches",
    "field_label_matches",
    "is_clickable_control",
    "is_fillable_control",
    "label_matches",
    "tab_gone_envelope",
    "validate_click_target",
    "validate_write_text_target",
    "validate_url",
]
