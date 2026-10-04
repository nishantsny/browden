"""The Selenium backend's frame primitives: enter-or-roll-back, and no top-URL fallback.

The driver is a ``MagicMock``; ``_resolve_one_visible`` is stubbed to return a
fake ``<iframe>``. What's pinned is the backend's own contract: a frame is
recorded only after both checks pass, and any failure from the switch on puts
the driver back where it was.
"""
from unittest.mock import MagicMock

import pytest

from browden.web_navigator.interface import FrameFocusError
from browden.web_navigator.selenium_chrome.backend import SeleniumChromeBackend

TOP = "https://app.example.com/page"
CHILD = "https://app.example.com/widget"


class Refused(Exception):
    pass


def _iframe(src=CHILD):
    el = MagicMock(name="iframe")
    el.tag_name = "iframe"
    el.get_property.side_effect = lambda name: src if name == "src" else None
    return el


def _backend(el=None, frame_url=CHILD):
    drv = MagicMock(name="driver")
    drv.window_handles = ["h1"]
    drv.current_url = TOP
    if isinstance(frame_url, BaseException):
        drv.execute_script.side_effect = frame_url
    else:
        drv.execute_script.return_value = frame_url
    backend = SeleniumChromeBackend(profile_dir="/tmp/bg-unit-profile")
    backend._driver = drv
    backend._drv = lambda: drv  # skip the health check; the mock is "alive"
    backend._focused = "h1"
    backend._resolve_one_visible = lambda d, sel: el if el is not None else _iframe()
    return backend, drv


def _ok(*_):
    return None


def _refuse(*_):
    raise Refused("no")


def test_enter_frame_records_the_frame_only_after_both_checks_pass():
    backend, drv = _backend()
    seen = []
    result = backend.enter_frame("#child", lambda src: seen.append(("src", src)),
                                 lambda top, frame: seen.append(("landed", top, frame)))
    assert seen == [("src", CHILD), ("landed", TOP, CHILD)]
    assert result == {"frame_url": CHILD, "top_url": TOP}
    assert backend._frame_paths["h1"] == ["#child"]
    assert backend.in_frame() is True


def test_a_refused_src_never_switches():
    backend, drv = _backend()
    with pytest.raises(Refused):
        backend.enter_frame("#child", _refuse, _ok)
    drv.switch_to.frame.assert_not_called()
    assert backend.in_frame() is False


def test_a_src_less_frame_skips_the_src_check_and_is_judged_on_landing():
    backend, drv = _backend(el=_iframe(src=""))
    src_checks = []
    backend.enter_frame("#child", src_checks.append, _ok)
    assert src_checks == []
    drv.switch_to.frame.assert_called_once()


def test_a_refused_landing_rolls_back_and_records_nothing():
    backend, drv = _backend(frame_url="https://ads.other.com/frame")
    with pytest.raises(Refused):
        backend.enter_frame("#child", _ok, _refuse)
    drv.switch_to.default_content.assert_called()  # focus restored to the recorded path (top)
    assert backend._frame_paths.get("h1", []) == []
    assert backend.in_frame() is False


def test_a_url_script_failure_after_switching_rolls_back_too():
    # Finding 2: the script that reads the landed URL throwing used to leave the
    # driver inside the frame, recorded but never checked.
    backend, drv = _backend(frame_url=RuntimeError("script failed"))
    landed = []
    with pytest.raises(RuntimeError, match="script failed"):
        backend.enter_frame("#child", _ok, lambda *a: landed.append(a))
    assert landed == []  # never judged...
    drv.switch_to.default_content.assert_called()  # ...and never left focused
    assert backend.in_frame() is False


def test_a_rollback_one_level_down_returns_to_the_recorded_parent_frame():
    backend, drv = _backend()
    backend.enter_frame("#mid", _ok, _ok)
    drv.reset_mock()
    drv.find_elements.return_value = [MagicMock(name="mid-frame")]
    with pytest.raises(Refused):
        backend.enter_frame("#child", _ok, _refuse)
    assert backend._frame_paths["h1"] == ["#mid"]  # the parent frame is kept
    drv.find_elements.assert_called_with("css selector", "#mid")  # and re-entered


def test_document_url_inside_a_frame_refuses_rather_than_falling_back_to_the_top():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.execute_script.side_effect = RuntimeError("script failed")
    with pytest.raises(FrameFocusError):
        backend.document_url()


def test_document_url_at_the_top_still_falls_back_to_current_url():
    # e.g. a chrome:// page, where script can't run: the read policy special-cases it.
    backend, drv = _backend()
    drv.execute_script.side_effect = RuntimeError("script failed")
    assert backend.document_url() == TOP


def test_retreat_to_top_forgets_the_path_without_running_script():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.reset_mock()
    backend.retreat_to_top()
    drv.switch_to.default_content.assert_called_once()
    drv.execute_script.assert_not_called()
    assert backend.in_frame() is False
