from unittest.mock import MagicMock, PropertyMock, patch

from browser_guard.web_navigator.selenium_chrome.backend import (
    SINGLETON_FILES,
    SeleniumChromeBackend,
    _clear_stale_singletons,
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
