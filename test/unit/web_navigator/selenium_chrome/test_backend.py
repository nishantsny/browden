import urllib.error
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from safe_agent_browser.dependencies.selenium import NoSuchWindowException
from safe_agent_browser.web_navigator.interface import TabNotFoundError
import safe_agent_browser.web_navigator.selenium_chrome.backend as backend
from safe_agent_browser.web_navigator.selenium_chrome.backend import (
    SINGLETON_FILES,
    SeleniumChromeBackend,
    _chrome_args,
    _clear_stale_singletons,
    _find_chrome_binary,
    _launch_chrome,
    _wellknown_chrome_paths,
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


class FakeChromeOS:
    """The OS boundary under a Chrome launch, and nothing else.

    Fakes the process spawn (``subprocess.Popen``), the DevTools HTTP probe
    (``urlopen``), the free-port lookup and where the Chrome binary is. Everything
    safe-agent-browser does on top of those — ``_launch_chrome``, ``_chrome_args``,
    ``_clear_stale_singletons``, ``_wait_for_devtools``, ``_terminate`` — runs for
    real. ``spawned`` records ``(argv, proc)`` per launch; ``on_spawn(argv)`` runs
    at the moment of the spawn.
    """

    def __init__(self, monkeypatch, port=9222):
        self.port = port
        self.devtools_up = True
        self.spawned: list[tuple[list[str], MagicMock]] = []
        self.on_spawn = None
        monkeypatch.setenv("SAFE_AGENT_BROWSER_CHROME_BINARY", "/fake/chrome")
        monkeypatch.setattr(backend, "get_free_port", lambda: self.port)
        monkeypatch.setattr(backend.subprocess, "Popen", self._popen)
        monkeypatch.setattr(backend.urllib.request, "urlopen", self._urlopen)

    def _popen(self, argv, **kwargs):
        if self.on_spawn is not None:
            self.on_spawn(argv)
        proc = MagicMock(name="chrome_proc")
        proc.poll.return_value = None  # running
        self.spawned.append((argv, proc))
        return proc

    def _urlopen(self, url, timeout=None):
        if not self.devtools_up:
            raise urllib.error.URLError("connection refused")
        resp = MagicMock(status=200)
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    @property
    def argv(self) -> list[str]:
        return self.spawned[-1][0]


@pytest.fixture
def chrome_os(monkeypatch):
    return FakeChromeOS(monkeypatch)


def _debugger_address(mock_webdriver):
    """The debuggerAddress experimental option Selenium was attached with."""
    opts = mock_webdriver.Chrome.call_args.kwargs["options"]
    return opts.experimental_options["debuggerAddress"]


@patch("safe_agent_browser.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_lazy_init(mock_webdriver, chrome_os):
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
    assert len(chrome_os.spawned) == 1  # one real launch


@patch("safe_agent_browser.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_launches_with_provided_profile_dir(mock_webdriver, chrome_os, tmp_path):
    chrome_os.port = 7000
    mock_webdriver.Chrome.return_value = _make_fake_driver()
    profile = tmp_path / "custom-profile"
    backend = _backend(profile_dir=str(profile))

    assert backend.get_profile_dir() == profile

    backend._drv()

    assert len(chrome_os.spawned) == 1
    assert f"--user-data-dir={profile}" in chrome_os.argv
    # Selenium attaches to the launched Chrome rather than spawning its own.
    assert _debugger_address(mock_webdriver) == "127.0.0.1:7000"


@patch("safe_agent_browser.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_recreates_after_dead_session(mock_webdriver, chrome_os):
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
    assert len(chrome_os.spawned) == 1  # one real launch


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
    monkeypatch.setenv("SAFE_AGENT_BROWSER_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.delenv("SAFE_AGENT_BROWSER_HEADLESS", raising=False)
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
    # Software WebGL fallback is always on: a GPU-less host must still expose a
    # WebGL context, or anti-bot sensors (Akamai) flag the session — issue #29.
    assert "--enable-unsafe-swiftshader" in args


def test_chrome_args_enable_swiftshader_when_headless(tmp_path, monkeypatch):
    monkeypatch.setenv("SAFE_AGENT_BROWSER_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.setenv("SAFE_AGENT_BROWSER_HEADLESS", "1")
    args = _chrome_args(tmp_path / "profile", 9222)
    # The WebGL fallback matters headless too (CI, containers): keep it on.
    assert "--enable-unsafe-swiftshader" in args


def test_chrome_args_headless_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SAFE_AGENT_BROWSER_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.setenv("SAFE_AGENT_BROWSER_HEADLESS", "1")
    monkeypatch.delenv("SAFE_AGENT_BROWSER_NO_SANDBOX", raising=False)
    args = _chrome_args(tmp_path / "profile", 9222)
    assert "--headless=new" in args
    # Headless does NOT imply --no-sandbox: the sandbox stays on unless opted out.
    assert "--no-sandbox" not in args


def test_chrome_args_no_sandbox_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("SAFE_AGENT_BROWSER_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.delenv("SAFE_AGENT_BROWSER_NO_SANDBOX", raising=False)
    # Off by default, even headless — the sandbox is kept.
    monkeypatch.setenv("SAFE_AGENT_BROWSER_HEADLESS", "1")
    assert "--no-sandbox" not in _chrome_args(tmp_path / "profile", 9222)
    # Explicit opt-in adds it (the escape hatch for sandbox-less hosts).
    monkeypatch.setenv("SAFE_AGENT_BROWSER_NO_SANDBOX", "1")
    assert "--no-sandbox" in _chrome_args(tmp_path / "profile", 9222)


def test_find_chrome_binary_honours_env(monkeypatch):
    monkeypatch.setenv("SAFE_AGENT_BROWSER_CHROME_BINARY", "/opt/chrome/chrome")
    assert _find_chrome_binary() == "/opt/chrome/chrome"


def test_find_chrome_binary_searches_path(monkeypatch):
    monkeypatch.delenv("SAFE_AGENT_BROWSER_CHROME_BINARY", raising=False)
    monkeypatch.delenv("CHROME_BIN", raising=False)

    def fake_which(name):
        return "/usr/bin/google-chrome" if name == "google-chrome" else None

    monkeypatch.setattr(
        "safe_agent_browser.web_navigator.selenium_chrome.backend.shutil.which", fake_which
    )
    assert _find_chrome_binary() == "/usr/bin/google-chrome"


def test_find_chrome_binary_raises_when_missing(monkeypatch):
    monkeypatch.delenv("SAFE_AGENT_BROWSER_CHROME_BINARY", raising=False)
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr(
        "safe_agent_browser.web_navigator.selenium_chrome.backend.shutil.which",
        lambda name: None,
    )
    # No well-known install either — must raise, not silently return nothing.
    monkeypatch.setattr(backend.Path, "exists", lambda self: False)
    with pytest.raises(RuntimeError):
        _find_chrome_binary()


def test_find_chrome_binary_uses_wellknown_when_not_on_path(monkeypatch):
    # macOS/Windows: Chrome isn't on PATH but sits at a canonical install path.
    paths = _wellknown_chrome_paths()
    if not paths:
        pytest.skip("Linux has no well-known install paths (Chrome is found on PATH); "
                    "this runs on the macOS and Windows runners")
    monkeypatch.delenv("SAFE_AGENT_BROWSER_CHROME_BINARY", raising=False)
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr(backend.shutil, "which", lambda name: None)
    installed = paths[-1]  # only the last one exists: the earlier ones are skipped
    monkeypatch.setattr(backend.Path, "exists", lambda self: str(self) == installed)
    assert _find_chrome_binary() == installed


def test_wellknown_chrome_paths_macos():
    paths = _wellknown_chrome_paths("darwin", "posix")
    assert any(p.endswith("Contents/MacOS/Google Chrome") for p in paths)


def test_wellknown_chrome_paths_windows():
    paths = _wellknown_chrome_paths("win32", "nt")
    assert paths and all(p.endswith("chrome.exe") for p in paths)


def test_wellknown_chrome_paths_linux_is_empty():
    assert _wellknown_chrome_paths("linux", "posix") == []


def test_launch_chrome_clears_singletons_before_spawning(chrome_os, tmp_path):
    for name in SINGLETON_FILES:
        (tmp_path / name).write_text("stale")
    left_at_spawn = []
    chrome_os.on_spawn = lambda argv: left_at_spawn.extend(
        name for name in SINGLETON_FILES if (tmp_path / name).exists())

    proc, port = _launch_chrome(tmp_path)

    assert left_at_spawn == []  # stale locks cleared before Chrome was spawned
    assert (proc, port) == (chrome_os.spawned[0][1], chrome_os.port)
    assert chrome_os.argv[0] == "/fake/chrome"
    assert f"--remote-debugging-port={chrome_os.port}" in chrome_os.argv


def test_launch_chrome_terminates_when_devtools_never_comes_up(chrome_os, tmp_path, monkeypatch):
    chrome_os.devtools_up = False
    # A fake clock, so the DevTools wait times out without really waiting.
    now = [0.0]
    monkeypatch.setattr(backend.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(backend.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    with pytest.raises(RuntimeError, match="not ready"):
        _launch_chrome(tmp_path)
    chrome_os.spawned[0][1].terminate.assert_called_once()  # no orphaned Chrome


@patch("safe_agent_browser.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_swallows_quit_error_on_dead_driver(mock_webdriver, chrome_os):
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
    # as a clean TabNotFoundError, not the driver's stack-trace exception. The
    # tab-targeting methods that still take a handle (select_tab / close_tab) are
    # where that translation lives — get_page_source / reload / screenshot no
    # longer self-focus, so the caller's select_tab is the one gate.
    for call in (lambda: backend.select_tab("dead"),
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


@patch("safe_agent_browser.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_restarts_when_current_window_is_gone(mock_webdriver, chrome_os):
    # Initial driver that has lost its current window: window_handles still works,
    # but current_window_handle raises — exactly what _drv()'s health check probes.
    dead_drv = _make_fake_driver(handles=("h1",))
    type(dead_drv).current_window_handle = PropertyMock(side_effect=NoSuchWindowException("no such window"))

    # New driver to be created upon restart
    alive_drv = _make_fake_driver(handles=("new_h1",))
    alive_drv.current_window_handle = "new_h1"
    mock_webdriver.Chrome.return_value = alive_drv

    backend = _backend_with_driver(dead_drv)

    # Any driving call goes through _drv(), whose health check restarts the session.
    assert backend.list_handles() == ["new_h1"]
    dead_drv.quit.assert_called_once()
    assert mock_webdriver.Chrome.call_count == 1


def test_is_running_probes_session_only_not_current_window():
    # is_running and _drv share _session_alive, but is_running is the look-only
    # probe: it checks the session (window_handles), NOT the current window (that's
    # _drv's heal-able concern). A live session with a stale current window still
    # reports running — no teardown, no relaunch.
    drv = _make_fake_driver(handles=("h1",))
    type(drv).current_window_handle = PropertyMock(side_effect=NoSuchWindowException("no such window"))
    backend = _backend_with_driver(drv)

    assert backend.is_running() is True
    drv.quit.assert_not_called()  # look-only: never tears the session down


def test_is_running_false_and_tears_down_a_dead_session():
    drv = _make_fake_driver(dead=True)  # window_handles itself raises
    backend = _backend_with_driver(drv)

    assert backend.is_running() is False
    drv.quit.assert_called_once()  # dead session is torn down (but not relaunched)


@patch("safe_agent_browser.web_navigator.selenium_chrome.backend.webdriver")
def test_drv_recreates_when_window_handles_fails(mock_webdriver, chrome_os):
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


@patch("safe_agent_browser.web_navigator.selenium_chrome.backend.webdriver")
def test_list_tab_ids_returns_handles_without_switching(mock_webdriver, chrome_os):
    drv = _make_fake_driver(handles=("h1", "h2", "h3"))
    mock_webdriver.Chrome.return_value = drv
    backend = _backend()

    assert backend.list_handles() == ["h1", "h2", "h3"]
    drv.switch_to.window.assert_not_called()  # cheap: no per-tab focus changes
