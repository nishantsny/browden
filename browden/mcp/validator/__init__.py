"""Re-export validator symbols for convenient access."""
from .access_rule_set import BrowdenAccessRuleSet
from .allowlist import HostRuleMatcher, ReadPolicy
from .errors import SessionBusyError, ValidationError, tab_gone_envelope
from .intent import (
    ACTIVATION_KEYS,
    classify_anchor_target,
    field_id_matches,
    field_label_matches,
    is_clickable_control,
    is_fillable_control,
    is_focusable_control,
    label_matches,
)
from .popularity import PopularityAllowlist
from .runtime_configuration import BrowdenRuntimeConfiguration
from .tranco import TrancoList
from .read_gates import ensure_url_allowed, validate_url
from .write_gates import (
    check_action_host,
    validate_click_target,
    validate_press_key_target,
    validate_write_text_target,
)

__all__ = [
    "ACTIVATION_KEYS",
    "BrowdenAccessRuleSet",
    "BrowdenRuntimeConfiguration",
    "HostRuleMatcher",
    "PopularityAllowlist",
    "ReadPolicy",
    "SessionBusyError",
    "TrancoList",
    "ValidationError",
    "check_action_host",
    "classify_anchor_target",
    "ensure_url_allowed",
    "field_id_matches",
    "field_label_matches",
    "is_clickable_control",
    "is_fillable_control",
    "is_focusable_control",
    "label_matches",
    "tab_gone_envelope",
    "validate_click_target",
    "validate_press_key_target",
    "validate_write_text_target",
    "validate_url",
]
