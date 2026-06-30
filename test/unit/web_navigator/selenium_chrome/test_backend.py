from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from browser_guard.dependencies.selenium import NoSuchWindowException
from browser_guard.web_navigator.interface import PageNotFoundError
from browser_guard.web_navigator.selenium_chrome.backend import (
    SINGLETON_FILES,
    SeleniumChromeBackend,
    _clear_stale_singletons,
    _default_profile_dir,
)


def _make_fake_driver(handles=("h1",), dead=False):
    drv = MagicMock(name="driver")
    if dead:
        type(drv).window_handles = PropertyMock(
            side_effect=Exception("invalid session id")
        )
    else:
        drv.window_handles = list(handles)
    return drv


@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_lazy_init(mock_webdriver):
    fake = _make_fake_driver()
    mock_webdriver.Chrome.return_value = fake

    backend = SeleniumChromeBackend()
    assert backend._driver is None

    drv = backend._drv()
    assert drv is fake
    assert mock_webdriver.Chrome.call_count == 1

    drv2 = backend._drv()
    assert drv2 is fake
    assert mock_webdriver.Chrome.call_count == 1  # cached, not recreated


@patch("browser_guard.web_navigator.selenium_chrome.backend._clear_stale_singletons")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_uses_provided_profile_dir(mock_webdriver, mock_clear, tmp_path):
    mock_webdriver.Chrome.return_value = _make_fake_driver()
    profile = tmp_path / "custom-profile"
    backend = SeleniumChromeBackend(profile_dir=str(profile))

    assert backend.profile_dir == profile

    backend._drv()

    args = mock_webdriver.Chrome.call_args.kwargs["options"].arguments
    assert f"--user-data-dir={profile}" in args
    mock_clear.assert_called_once_with(profile)


@patch("browser_guard.web_navigator.selenium_chrome.backend.PROFILE_DIR")
@patch("browser_guard.web_navigator.selenium_chrome.backend._clear_stale_singletons")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_defaults_to_module_profile_dir(mock_webdriver, mock_clear, mock_profile_dir):
    mock_webdriver.Chrome.return_value = _make_fake_driver()
    backend = SeleniumChromeBackend()  # no profile_dir -> module default

    assert backend.profile_dir is mock_profile_dir

    backend._drv()
    args = mock_webdriver.Chrome.call_args.kwargs["options"].arguments
    assert f"--user-data-dir={mock_profile_dir}" in args


@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_recreates_after_dead_session(mock_webdriver):
    dead = _make_fake_driver(dead=True)
    alive = _make_fake_driver()
    mock_webdriver.Chrome.return_value = alive

    backend = SeleniumChromeBackend()
    backend._driver = dead  # simulate a cached, dead driver

    drv = backend._drv()

    assert drv is alive
    dead.quit.assert_called_once()
    assert mock_webdriver.Chrome.call_count == 1  # one new driver after the dead one


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


@patch("browser_guard.web_navigator.selenium_chrome.backend._clear_stale_singletons")
@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_clears_singletons_before_launch(mock_webdriver, mock_clear):
    mock_webdriver.Chrome.return_value = _make_fake_driver()
    backend = SeleniumChromeBackend()

    manager = MagicMock()
    manager.attach_mock(mock_clear, "clear")
    manager.attach_mock(mock_webdriver.Chrome, "Chrome")

    backend._drv()

    call_names = [c[0] for c in manager.mock_calls]
    assert call_names == ["clear", "Chrome"]


@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_swallows_quit_error_on_dead_driver(mock_webdriver):
    dead = _make_fake_driver(dead=True)
    dead.quit.side_effect = Exception("already gone")
    alive = _make_fake_driver()
    mock_webdriver.Chrome.return_value = alive

    backend = SeleniumChromeBackend()
    backend._driver = dead

    drv = backend._drv()
    assert drv is alive


def _backend_with_driver(drv):
    backend = SeleniumChromeBackend()
    backend._driver = drv
    return backend


def test_switch_failure_becomes_page_not_found():
    drv = _make_fake_driver(handles=("h1", "h2"))
    drv.switch_to.window.side_effect = NoSuchWindowException("no such window\n  (Session info: ...)")
    backend = _backend_with_driver(drv)

    for call in (lambda: backend.select_page("dead"),
                 lambda: backend.get_page_source("dead"),
                 lambda: backend.reload("dead"),
                 lambda: backend.close_page("dead")):
        with pytest.raises(PageNotFoundError) as exc:
            call()
        assert "dead" in str(exc.value)
        assert "Session info" not in str(exc.value)  # no driver stack trace leaks through


@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_current_page_id_triggers_restart_on_no_such_window(mock_webdriver):
    # Initial driver that has lost its current window
    dead_drv = _make_fake_driver(handles=("h1",))
    type(dead_drv).current_window_handle = PropertyMock(side_effect=NoSuchWindowException("no such window"))
    
    # New driver to be created upon restart
    alive_drv = _make_fake_driver(handles=("new_h1",))
    alive_drv.current_window_handle = "new_h1"
    mock_webdriver.Chrome.return_value = alive_drv

    backend = _backend_with_driver(dead_drv)
    
    # This should now trigger _drv() to restart and return the new handle
    assert backend.current_page_id() == "new_h1"
    dead_drv.quit.assert_called_once()
    assert mock_webdriver.Chrome.call_count == 1


@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_recreates_when_window_handles_fails(mock_webdriver):
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
    # Closing the focused tab leaves the driver on a dead handle; close_page must
    # re-focus a remaining window so the next command doesn't see a "dead" session.
    drv = _make_fake_driver(handles=("h1", "h2"))

    def _close():
        drv.window_handles = ["h1"]  # h2 is gone after drv.close()
    drv.close.side_effect = _close

    backend = _backend_with_driver(drv)
    backend.close_page("h2")

    drv.close.assert_called_once()
    # Last switch_to.window call targets a surviving handle, not the closed one.
    assert drv.switch_to.window.call_args.args == ("h1",)


def test_close_page_last_tab_is_refused():
    drv = _make_fake_driver(handles=("only",))
    backend = _backend_with_driver(drv)
    with pytest.raises(ValueError):
        backend.close_page("only")
    drv.close.assert_not_called()


def test_navigate_failure_becomes_page_not_found():
    drv = _make_fake_driver()
    drv.get.side_effect = NoSuchWindowException("no such window")
    backend = _backend_with_driver(drv)
    with pytest.raises(PageNotFoundError):
        backend.navigate("https://example.com")


@patch("browser_guard.web_navigator.selenium_chrome.backend.webdriver")
def test_list_page_ids_returns_handles_without_switching(mock_webdriver):
    drv = _make_fake_driver(handles=("h1", "h2", "h3"))
    mock_webdriver.Chrome.return_value = drv
    backend = SeleniumChromeBackend()

    assert backend.list_page_ids() == ["h1", "h2", "h3"]
    drv.switch_to.window.assert_not_called()  # cheap: no per-tab focus changes
