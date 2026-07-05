from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from browser_guard.dependencies.selenium import NoSuchWindowException
from browser_guard.web_navigator.interface import TabNotFoundError
from browser_guard.web_navigator.selenium_chrome.backend import (
    SINGLETON_FILES,
    SeleniumChromeBackend,
    _chrome_args,
    _clear_stale_singletons,
    _default_profile_dir,
    _find_chrome_binary,
    _launch_chrome,
)


# The backend deals in raw Selenium handles — no namespace. profile_dir is
# required, defaulted here so tests that don't care can omit it.
def _backend(**kwargs):
    kwargs.setdefault("profile_dir", "/tmp/bg-unit-profile")
    return SeleniumChromeBackend(**kwargs)


def _make_fake_driver(handles=("h1",), dead=False):
    drv = MagicMock(name="driver")
    if dead:
        type(drv).window_handles = PropertyMock(
            side_effect=Exception("invalid session id")
        )
    else:
        drv.window_handles = list(handles)
    return drv


def _patch_launch(mock_launch, port=9222):
    """Make _launch_chrome return a fresh fake (proc, port) pair per call."""
    mock_launch.side_effect = lambda profile: (MagicMock(name="chrome_proc"), port)


def _debugger_address(mock_webdriver):
    """The debuggerAddress experimental option Selenium was attached with."""
    opts = mock_webdriver.Chrome.call_args.kwargs["options"]
    return opts.experimental_options["debuggerAddress"]


@patch("browser_guard.web_navigator.selenium_chrome.backend._launch_chrome")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_lazy_init(mock_webdriver, mock_launch):
    _patch_launch(mock_launch)
    fake = _make_fake_driver()
    mock_webdriver.Chrome.return_value = fake

    backend = _backend()
    assert backend._driver is None

    drv = backend._drv()
    assert drv is fake
    assert mock_webdriver.Chrome.call_count == 1

    drv2 = backend._drv()
    assert drv2 is fake
    assert mock_webdriver.Chrome.call_count == 1  # cached, not recreated
    assert mock_launch.call_count == 1


@patch("browser_guard.web_navigator.selenium_chrome.backend._launch_chrome")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_launches_with_provided_profile_dir(mock_webdriver, mock_launch, tmp_path):
    _patch_launch(mock_launch, port=7000)
    mock_webdriver.Chrome.return_value = _make_fake_driver()
    profile = tmp_path / "custom-profile"
    backend = _backend(profile_dir=str(profile))

    assert backend.get_profile_dir() == profile

    backend._drv()

    mock_launch.assert_called_once_with(profile)
    # Selenium attaches to the launched Chrome rather than spawning its own.
    assert _debugger_address(mock_webdriver) == "127.0.0.1:7000"


@patch("browser_guard.web_navigator.selenium_chrome.backend._launch_chrome")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_recreates_after_dead_session(mock_webdriver, mock_launch):
    _patch_launch(mock_launch)
    dead = _make_fake_driver(dead=True)
    alive = _make_fake_driver()
    mock_webdriver.Chrome.return_value = alive

    backend = _backend()
    backend._driver = dead  # simulate a cached, dead driver
    dead_proc = MagicMock(name="dead_chrome")
    dead_proc.poll.return_value = None  # still "running" so _terminate acts
    backend._chrome_proc = dead_proc

    drv = backend._drv()

    assert drv is alive
    dead.quit.assert_called_once()
    dead_proc.terminate.assert_called_once()  # the old Chrome is reaped
    assert mock_webdriver.Chrome.call_count == 1  # one new driver after the dead one
    assert mock_launch.call_count == 1


def test_default_profile_dir_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert _default_profile_dir() == tmp_path / "browser-guard" / "chrome-profile"


def test_default_profile_dir_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _default_profile_dir() == tmp_path / ".cache" / "browser-guard" / "chrome-profile"


def test_clear_stale_singletons_removes_files(tmp_path):
    for name in SINGLETON_FILES:
        (tmp_path / name).write_text("stale")
    (tmp_path / "Default").mkdir()  # unrelated content must survive

    _clear_stale_singletons(tmp_path)

    for name in SINGLETON_FILES:
        assert not (tmp_path / name).exists()
    assert (tmp_path / "Default").is_dir()


def test_clear_stale_singletons_noop_when_absent(tmp_path):
    _clear_stale_singletons(tmp_path)  # must not raise on empty dir


def test_chrome_args_have_no_automation_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_GUARD_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.delenv("BROWSER_GUARD_HEADLESS", raising=False)
    args = _chrome_args(tmp_path / "profile", 9222)

    assert args[0] == "/usr/bin/google-chrome"
    assert f"--user-data-dir={tmp_path / 'profile'}" in args
    assert "--remote-debugging-port=9222" in args
    # None of chromedriver's automation tells should appear — that's the point.
    joined = " ".join(args)
    assert "enable-automation" not in joined
    assert "AutomationControlled" not in joined
    assert "--test-type" not in joined
    # Headed by default: no headless switch unless asked for.
    assert not any(a.startswith("--headless") for a in args)


def test_chrome_args_headless_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_GUARD_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.setenv("BROWSER_GUARD_HEADLESS", "1")
    args = _chrome_args(tmp_path / "profile", 9222)
    assert "--headless=new" in args
    assert "--no-sandbox" in args


def test_find_chrome_binary_honours_env(monkeypatch):
    monkeypatch.setenv("BROWSER_GUARD_CHROME_BINARY", "/opt/chrome/chrome")
    assert _find_chrome_binary() == "/opt/chrome/chrome"


def test_find_chrome_binary_searches_path(monkeypatch):
    monkeypatch.delenv("BROWSER_GUARD_CHROME_BINARY", raising=False)
    monkeypatch.delenv("CHROME_BIN", raising=False)

    def fake_which(name):
        return "/usr/bin/google-chrome" if name == "google-chrome" else None

    monkeypatch.setattr(
        "browser_guard.web_navigator.selenium_chrome.backend.shutil.which", fake_which
    )
    assert _find_chrome_binary() == "/usr/bin/google-chrome"


def test_find_chrome_binary_raises_when_missing(monkeypatch):
    monkeypatch.delenv("BROWSER_GUARD_CHROME_BINARY", raising=False)
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr(
        "browser_guard.web_navigator.selenium_chrome.backend.shutil.which",
        lambda name: None,
    )
    with pytest.raises(RuntimeError):
        _find_chrome_binary()


@patch("browser_guard.web_navigator.selenium_chrome.backend._wait_for_devtools")
@patch("browser_guard.web_navigator.selenium_chrome.backend.subprocess")
@patch("browser_guard.web_navigator.selenium_chrome.backend._free_port", return_value=4321)
@patch("browser_guard.web_navigator.selenium_chrome.backend._chrome_args", return_value=["chrome"])
@patch("browser_guard.web_navigator.selenium_chrome.backend._clear_stale_singletons")
def test_launch_chrome_clears_singletons_before_spawning(
    mock_clear, mock_args, mock_port, mock_subprocess, mock_wait, tmp_path
):
    proc = MagicMock(name="proc")
    mock_subprocess.Popen.return_value = proc

    manager = MagicMock()
    manager.attach_mock(mock_clear, "clear")
    manager.attach_mock(mock_subprocess.Popen, "Popen")

    out_proc, port = _launch_chrome(tmp_path)

    assert (out_proc, port) == (proc, 4321)
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names == ["clear", "Popen"]  # stale lock cleared before spawn
    mock_wait.assert_called_once()


@patch("browser_guard.web_navigator.selenium_chrome.backend._wait_for_devtools")
@patch("browser_guard.web_navigator.selenium_chrome.backend._terminate")
@patch("browser_guard.web_navigator.selenium_chrome.backend.subprocess")
@patch("browser_guard.web_navigator.selenium_chrome.backend._free_port", return_value=4321)
@patch("browser_guard.web_navigator.selenium_chrome.backend._chrome_args", return_value=["chrome"])
@patch("browser_guard.web_navigator.selenium_chrome.backend._clear_stale_singletons")
def test_launch_chrome_terminates_when_devtools_never_comes_up(
    mock_clear, mock_args, mock_port, mock_subprocess, mock_terminate, mock_wait, tmp_path
):
    proc = MagicMock(name="proc")
    mock_subprocess.Popen.return_value = proc
    mock_wait.side_effect = RuntimeError("never came up")

    with pytest.raises(RuntimeError):
        _launch_chrome(tmp_path)
    mock_terminate.assert_called_once_with(proc)  # no orphaned Chrome


@patch("browser_guard.web_navigator.selenium_chrome.backend._launch_chrome")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_swallows_quit_error_on_dead_driver(mock_webdriver, mock_launch):
    _patch_launch(mock_launch)
    dead = _make_fake_driver(dead=True)
    dead.quit.side_effect = Exception("already gone")
    alive = _make_fake_driver()
    mock_webdriver.Chrome.return_value = alive

    backend = _backend()
    backend._driver = dead

    drv = backend._drv()
    assert drv is alive


def _backend_with_driver(drv):
    backend = _backend()
    backend._driver = drv
    return backend


def test_switch_failure_becomes_page_not_found():
    drv = _make_fake_driver(handles=("h1", "h2"))
    drv.switch_to.window.side_effect = NoSuchWindowException("no such window\n  (Session info: ...)")
    backend = _backend_with_driver(drv)

    # page_ids are raw Selenium handles; a handle Selenium rejects must surface
    # as a clean TabNotFoundError, not the driver's stack-trace exception.
    for call in (lambda: backend.select_tab("dead"),
                 lambda: backend.get_page_source("dead"),
                 lambda: backend.reload("dead"),
                 lambda: backend.close_tab("dead")):
        with pytest.raises(TabNotFoundError) as exc:
            call()
        assert "dead" in str(exc.value)
        assert "Session info" not in str(exc.value)  # no driver stack trace leaks through


def test_profile_dir_is_required():
    with pytest.raises(TypeError):
        SeleniumChromeBackend()  # profile_dir is required
    with pytest.raises(ValueError):
        SeleniumChromeBackend(None)  # must be non-empty
    with pytest.raises(ValueError):
        SeleniumChromeBackend("")  # must be non-empty


def test_get_profile_dir_returns_construction_path(tmp_path):
    backend = SeleniumChromeBackend(profile_dir=str(tmp_path / "prof"))
    assert backend.get_profile_dir() == tmp_path / "prof"


@patch("browser_guard.web_navigator.selenium_chrome.backend._launch_chrome")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_current_tab_id_triggers_restart_on_no_such_window(mock_webdriver, mock_launch):
    _patch_launch(mock_launch)
    # Initial driver that has lost its current window
    dead_drv = _make_fake_driver(handles=("h1",))
    type(dead_drv).current_window_handle = PropertyMock(side_effect=NoSuchWindowException("no such window"))

    # New driver to be created upon restart
    alive_drv = _make_fake_driver(handles=("new_h1",))
    alive_drv.current_window_handle = "new_h1"
    mock_webdriver.Chrome.return_value = alive_drv

    backend = _backend_with_driver(dead_drv)

    # This should now trigger _drv() to restart and return the new handle
    assert backend.current_tab_id() == "new_h1"
    dead_drv.quit.assert_called_once()
    assert mock_webdriver.Chrome.call_count == 1


@patch("browser_guard.web_navigator.selenium_chrome.backend._launch_chrome")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_recreates_when_window_handles_fails(mock_webdriver, mock_launch):
    _patch_launch(mock_launch)
    # Driver that fails on window_handles (session dead)
    dead_drv = _make_fake_driver(dead=True)

    # New driver
    alive_drv = _make_fake_driver(handles=("h1",))
    mock_webdriver.Chrome.return_value = alive_drv

    backend = _backend_with_driver(dead_drv)

    drv = backend._drv()
    assert drv is alive_drv
    dead_drv.quit.assert_called_once()
    assert mock_webdriver.Chrome.call_count == 1


def test_close_page_refocuses_a_survivor():
    # Closing the focused tab leaves the driver on a dead handle; close_tab must
    # re-focus a remaining window so the next command doesn't see a "dead" session.
    drv = _make_fake_driver(handles=("h1", "h2"))

    def _close():
        drv.window_handles = ["h1"]  # h2 is gone after drv.close()
    drv.close.side_effect = _close

    backend = _backend_with_driver(drv)
    backend.close_tab("h2")

    drv.close.assert_called_once()
    # Last switch_to.window call targets a surviving handle, not the closed one.
    assert drv.switch_to.window.call_args.args == ("h1",)


def test_close_page_last_tab_is_refused():
    drv = _make_fake_driver(handles=("only",))
    backend = _backend_with_driver(drv)
    with pytest.raises(ValueError):
        backend.close_tab("only")
    drv.close.assert_not_called()


def test_navigate_failure_becomes_page_not_found():
    drv = _make_fake_driver()
    drv.get.side_effect = NoSuchWindowException("no such window")
    backend = _backend_with_driver(drv)
    with pytest.raises(TabNotFoundError):
        backend.navigate("https://example.com")


@patch("browser_guard.web_navigator.selenium_chrome.backend._launch_chrome")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_list_tab_ids_returns_handles_without_switching(mock_webdriver, mock_launch):
    _patch_launch(mock_launch)
    drv = _make_fake_driver(handles=("h1", "h2", "h3"))
    mock_webdriver.Chrome.return_value = drv
    backend = _backend()

    assert backend.list_tab_ids() == ["h1", "h2", "h3"]
    drv.switch_to.window.assert_not_called()  # cheap: no per-tab focus changes
