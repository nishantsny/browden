"""configs/samples/allow_iframe_access.yaml says what it allows — keep it true.

The sample documents, in comments, which frames on its example page can be
entered and where writes apply. These run each of those claims through the real
gates, so the sample can't drift from the code.
"""
from pathlib import Path

import pytest

from browden.configs.loader import load_runtime_configuration
from browden.mcp.validator import ValidationError, check_action_host, frame_gate

SAMPLE = Path(__file__).resolve().parents[3] / "configs" / "samples" / "allow_iframe_access.yaml"
TOP = "https://billing.example.com/account"
HISTORY = "https://billing.example.com/widgets/payment-history"


@pytest.fixture(scope="module")
def rules():
    return load_runtime_configuration(SAMPLE).access_rules_for("/any/profile")


@pytest.mark.parametrize("landed", [
    HISTORY,  # the same-origin widget
    TOP,      # the srcdoc #help panel, judged as the page that wrote it
])
def test_the_samples_same_origin_frames_can_be_entered(rules, landed):
    assert frame_gate(rules).check_landed(TOP, landed) is None


@pytest.mark.parametrize("landed", [
    "https://chat.example.net/embed",                          # readable, other origin
    "http://billing.example.com/widgets/payment-history",      # other scheme
    "https://www.billing.example.com/widgets/payment-history",  # other host
])
def test_the_samples_other_origin_frames_are_refused(rules, landed):
    with pytest.raises(ValidationError, match="cross-origin"):
        frame_gate(rules).check_landed(TOP, landed)


def test_the_chat_embed_is_readable_as_a_top_page_all_the_same(rules):
    assert frame_gate(rules).check_page("https://chat.example.net/embed") is None


@pytest.mark.parametrize("url", [HISTORY, TOP])
def test_clicks_are_authorized_by_the_frames_own_url(rules, url):
    assert check_action_host(rules, "click", url) is None


def test_no_click_rule_reaches_an_unlisted_path(rules):
    with pytest.raises(ValidationError):
        check_action_host(rules, "click", "https://billing.example.com/settings")


def test_typing_is_authorized_only_inside_the_widget(rules):
    assert check_action_host(rules, "write-text", HISTORY) is None
    with pytest.raises(ValidationError):
        check_action_host(rules, "write-text", TOP)
