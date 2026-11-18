"""The software-WebGL switch: what reaches Chrome's command line, and when it relaunches.

The flag can only be chosen as Chrome starts, so the whole feature is about
launches: which argv the next one gets, whether a running browser is replaced to
get there, and what the backend forgets when it is. Chrome itself is faked at the
process boundary (Popen, the DevTools probe, the driver), so these tests pin the
*decisions* — that the ordinary path carries no WebGL flag at all, that asking
for the mode already in force launches nothing, and that a relaunch drops the
state that belonged to the dead browser.

What they deliberately do not pin is whether the flag makes WebGL work: that is
Chrome's behaviour on a given host, measured headful on the deployment box, and
unobservable in a headless CI run where Chrome rasterises WebGL in software
regardless. See test/e2e/test_software_webgl.py for the end-to-end half.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from browden.web_navigator.selenium_chrome import backend as backend_mod
from browden.web_navigator.selenium_chrome.backend import SeleniumChromeBackend, _chrome_args

PORT = 41000


def _devtools_response(port):
    """A 200 from /json/version, which is all _wait_for_devtools needs to proceed."""
    resp = MagicMock(status=200)
    resp.read.return_value = json.dumps(
        {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/x"}).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    return resp


class FakeChromeLaunch:
    """Chrome at the process boundary: records every launch's argv.

    ``launches`` is the list of command lines handed to Popen, newest last, so a
    test can ask what the next launch decided without a browser existing.
    """

    def __init__(self, monkeypatch):
        self.launches: list[list[str]] = []
        self.processes: list[MagicMock] = []
        monkeypatch.setenv("BROWDEN_CHROME_BINARY", "/usr/bin/google-chrome")
        monkeypatch.delenv("BROWDEN_HEADLESS", raising=False)
        monkeypatch.setattr(backend_mod, "_free_port", lambda: PORT)
        monkeypatch.setattr(backend_mod.subprocess, "Popen", self._popen)
        monkeypatch.setattr(backend_mod.urllib.request, "urlopen",
                            lambda *a, **k: _devtools_response(PORT))

    def _popen(self, args, **kwargs):
        self.launches.append(list(args))
        proc = MagicMock()
        proc.poll.return_value = None          # still running
        self.processes.append(proc)
        return proc

    @property
    def count(self):
        return len(self.launches)

    def argv(self, index=-1):
        return self.launches[index]


@pytest.fixture
def chrome(monkeypatch):
    return FakeChromeLaunch(monkeypatch)


class _Driver(MagicMock):
    """A driver whose session can be killed: set ``dead`` and its probes raise.

    ``window_handles`` is what the backend's liveness probe reads, so a property
    is the only way to make the session fail the way a vanished browser does.
    """
    dead = False

    @property
    def window_handles(self):
        if self.dead:
            raise Exception("no such session: browser gone")
        return ["h1"]


@pytest.fixture
def driver(monkeypatch):
    """The Selenium driver that attaches to the launched Chrome."""
    drivers = []

    def make_driver(**kwargs):
        drv = _Driver()
        drv.current_window_handle = "h1"
        drv.title = "t"
        drv.current_url = "about:blank"
        drivers.append(drv)
        return drv

    monkeypatch.setattr(backend_mod.webdriver, "Chrome", make_driver)
    return drivers


@pytest.fixture
def be(tmp_path, chrome, driver):
    b = SeleniumChromeBackend(tmp_path / "profile")
    yield b
    b.shutdown()


def test_a_new_backend_starts_without_software_webgl(be):
    # The default is off: the flag is opt-in per session now.
    assert be.software_webgl_enabled() is False


def test_the_ordinary_launch_carries_no_webgl_flag(be, chrome):
    be.new_blank_tab()

    assert chrome.count == 1
    assert "--enable-unsafe-swiftshader" not in chrome.argv()


def test_asking_for_software_webgl_puts_the_flag_on_the_next_launch(be, chrome):
    be.set_software_webgl(True)
    be.new_blank_tab()

    assert chrome.count == 1
    assert "--enable-unsafe-swiftshader" in chrome.argv()


def test_setting_the_mode_before_any_launch_relaunches_nothing(be, chrome):
    # Nothing is running yet, so there is nothing to replace: the mode is simply
    # remembered, and the caller is told no relaunch happened.
    assert be.set_software_webgl(True) is False
    assert chrome.count == 0
    assert be.software_webgl_enabled() is True


def test_changing_the_mode_of_a_running_browser_relaunches_it(be, chrome):
    be.new_blank_tab()
    assert chrome.count == 1

    assert be.set_software_webgl(True) is True

    # The old Chrome is stopped at once; the next driver call starts the new one.
    chrome.processes[0].terminate.assert_called()
    be.new_blank_tab()
    assert chrome.count == 2
    assert "--enable-unsafe-swiftshader" in chrome.argv()


def test_asking_for_the_mode_already_in_force_does_nothing(be, chrome):
    be.set_software_webgl(True)
    be.new_blank_tab()
    assert chrome.count == 1

    assert be.set_software_webgl(True) is False

    # No relaunch, and the live browser is untouched — which is what makes it
    # safe for a caller to pass its preference on every tab it opens.
    assert chrome.count == 1
    chrome.processes[0].terminate.assert_not_called()


def test_turning_software_webgl_back_off_relaunches_without_the_flag(be, chrome):
    be.set_software_webgl(True)
    be.new_blank_tab()
    assert "--enable-unsafe-swiftshader" in chrome.argv()

    assert be.set_software_webgl(False) is True
    be.new_blank_tab()

    assert chrome.count == 2
    assert "--enable-unsafe-swiftshader" not in chrome.argv()
    assert be.software_webgl_enabled() is False


def test_the_mode_survives_healing_a_dead_browser(be, chrome, driver):
    be.set_software_webgl(True)
    be.new_blank_tab()

    # Chrome dies on its own; the next call heals the session by relaunching.
    driver[0].dead = True
    assert be.is_running() is False

    be.new_blank_tab()

    # A session that asked for software WebGL keeps it: the heal must not
    # silently drop the caller's choice.
    assert chrome.count == 2
    assert "--enable-unsafe-swiftshader" in chrome.argv()
    assert be.software_webgl_enabled() is True


def test_a_relaunch_forgets_the_incognito_context(be, chrome):
    # The context lived in the Chrome that just died; reusing its id against the
    # new browser's port would be a bug, so _teardown drops it.
    with patch.object(backend_mod.cdp, "create_browser_context", return_value="CTX1"), \
         patch.object(backend_mod.cdp, "create_target", return_value="TGT1"):
        be.new_blank_tab(incognito=True)
    assert be._incognito_context == "CTX1"

    be.set_software_webgl(True)

    assert be._incognito_context is None
    assert be._incognito_handles == set()
    assert be._devtools_port is None


def test_the_flag_is_the_only_difference_between_the_two_launches(be, chrome):
    be.new_blank_tab()
    be.set_software_webgl(True)
    be.new_blank_tab()

    off, on = chrome.argv(0), chrome.argv(1)
    # Everything else about a human-looking Chrome stays identical, so turning
    # WebGL on cannot quietly change the profile, the port policy or the mode.
    assert [a for a in on if a != "--enable-unsafe-swiftshader"] == off


def test_the_flag_is_inserted_without_disturbing_the_rest_of_the_line(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWDEN_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.delenv("BROWDEN_HEADLESS", raising=False)

    base = _chrome_args(tmp_path / "p", 9222)
    with_flag = _chrome_args(tmp_path / "p", 9222, software_webgl=True)

    # Not "the flag comes last": what follows it is whatever the host asks for
    # (--password-store=..., --no-sandbox, the headless block), which differs by
    # OS and by runner. The invariant is narrower and true everywhere — turning
    # WebGL on adds that one argument and leaves every other one in place, in
    # order, so the mode cannot quietly change the profile or the port policy.
    assert [a for a in with_flag if a != "--enable-unsafe-swiftshader"] == base
    assert (with_flag.index("--enable-unsafe-swiftshader")
            > with_flag.index("--no-default-browser-check"))
