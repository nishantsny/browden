"""Incognito tabs: the browser-level CDP calls, and the context's lifetime.

An incognito tab is a page target in an incognito *browser context*, created
over Chrome's browser-level DevTools socket because chromedriver's own CDP
session is page-scoped and refuses ``Target.createBrowserContext``. These tests
fake that socket — the external boundary — and let ``cdp.py``'s real framing,
id-matching and error handling run on top of it.

What matters here and cannot be asserted in e2e: that the ordinary tab path is
completely untouched (no CDP at all), that one context is shared by a session's
incognito tabs rather than one per tab, and that the context is disposed when
its last tab closes and forgotten when the browser dies.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import browden.web_navigator.selenium_chrome.backend as backend
import browden.web_navigator.selenium_chrome.cdp as cdp


class FakeChromeLaunch:
    """The OS boundary under a Chrome launch: spawn, DevTools probe, port, binary.

    A local twin of ``test_backend.FakeChromeOS`` — the test directory has no
    package, so its fakes cannot be imported from here. Kept deliberately
    minimal: only what a launch needs so that ``_launch_chrome`` and
    ``_wait_for_devtools`` can run for real underneath these tests.
    """

    def __init__(self, monkeypatch, port=9222):
        self.port = port
        monkeypatch.setenv("BROWDEN_CHROME_BINARY", "/fake/chrome")
        monkeypatch.setattr(backend, "get_free_port", lambda: port)
        monkeypatch.setattr(backend.subprocess, "Popen", self._popen)
        monkeypatch.setattr(backend.urllib.request, "urlopen", self._urlopen)

    def _popen(self, argv, **kwargs):
        proc = MagicMock(name="chrome_proc")
        proc.poll.return_value = None            # running
        return proc

    def _urlopen(self, url, timeout=None):
        return _devtools_response(self.port)


@pytest.fixture
def chrome_os(monkeypatch):
    return FakeChromeLaunch(monkeypatch)


def _devtools_response(port: int):
    """A /json/version response both consumers accept.

    ``_wait_for_devtools`` reads ``.status``; ``cdp._browser_ws_url`` reads
    ``.read()``. Both go through the one global ``urllib.request.urlopen``, so a
    response that answers only one of them leaves the other spinning.
    """
    payload = json.dumps(
        {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc"})
    resp = MagicMock(status=200)
    resp.read.return_value = payload.encode()
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def _make_fake_driver(handles=("h1",)):
    """A driver mock that actually moves focus, the way a real one does.

    ``switch_to.window(h)`` makes ``h`` the current window — ``_tabinfo`` reads
    the tab it describes off exactly that, so a fake that ignores focus would
    report every tab as the first one.
    """
    drv = MagicMock(name="driver")
    drv.window_handles = list(handles)
    drv.current_window_handle = handles[0]

    def focus(handle):
        drv.current_window_handle = handle

    drv.switch_to.window.side_effect = focus
    return drv


class FakeBrowserSocket:
    """One websocket to Chrome's browser target, as `websocket-client` shapes it."""

    def __init__(self, devtools: "FakeDevTools"):
        self._devtools = devtools
        self._outbox: list[str] = []
        self.closed = False

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self._devtools.calls.append((message["method"], message.get("params", {})))
        # An unsolicited event, to prove the reply loop skips what it did not ask
        # for instead of mistaking the first frame for its answer.
        self._outbox.append(json.dumps(
            {"method": "Target.targetCreated", "params": {"targetInfo": {}}}))
        self._outbox.append(json.dumps(
            self._devtools.reply_to(message["id"], message["method"])))

    def recv(self) -> str:
        return self._outbox.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeDevTools:
    """The browser-level DevTools boundary: the /json/version probe and the socket.

    Everything above it — ``cdp.create_browser_context``, ``create_target``,
    ``dispose_browser_context``, their framing and error translation — runs for
    real.
    """

    def __init__(self, monkeypatch, port=9222):
        self.port = port
        self.calls: list[tuple[str, dict]] = []
        self.sockets: list[FakeBrowserSocket] = []
        self.origins: list[str] = []
        self.probed: list[str] = []
        self.next_context = iter(f"CTX{n}" for n in range(1, 99))
        self.next_target = iter(f"TGT{n}" for n in range(1, 99))
        self.dispose_error: dict | None = None
        monkeypatch.setattr(cdp.urllib.request, "urlopen", self._urlopen)
        monkeypatch.setattr(cdp, "create_connection", self._connect)

    def _urlopen(self, url, timeout=None):
        assert url.endswith("/json/version"), url
        self.probed.append(url)
        return _devtools_response(self.port)

    def _connect(self, url, origin=None, timeout=None):
        self.origins.append(origin)
        sock = FakeBrowserSocket(self)
        self.sockets.append(sock)
        return sock

    def reply_to(self, id: int, method: str) -> dict:
        if method == "Target.createBrowserContext":
            return {"id": id, "result": {"browserContextId": next(self.next_context)}}
        if method == "Target.createTarget":
            return {"id": id, "result": {"targetId": next(self.next_target)}}
        if method == "Target.disposeBrowserContext":
            if self.dispose_error is not None:
                return {"id": id, "error": self.dispose_error}
            return {"id": id, "result": {}}
        raise AssertionError(f"unexpected CDP method {method}")

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


@pytest.fixture
def devtools(monkeypatch):
    return FakeDevTools(monkeypatch)


def _running_backend(mock_webdriver, handles=("h1",)):
    """A backend with a live (faked) Chrome already attached."""
    drv = _make_fake_driver(handles=handles)
    drv.current_window_handle = handles[0]
    drv.current_url = "about:blank"
    drv.title = ""
    mock_webdriver.Chrome.return_value = drv
    b = backend.SeleniumChromeBackend(profile_dir="/tmp/bg-unit-profile")
    b._drv()
    return b, drv


# --- the ordinary path must not change -------------------------------------

@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_a_normal_tab_touches_no_cdp_at_all(mock_webdriver, chrome_os, devtools):
    # The default is the whole point of the flag being opt-in: an ordinary tab
    # must not pay a socket, a round trip, or a new failure mode.
    b, drv = _running_backend(mock_webdriver)

    tab = b.new_blank_tab()

    drv.switch_to.new_window.assert_called_once_with("tab")
    assert devtools.calls == []
    assert devtools.sockets == []
    assert tab.incognito is False


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_incognito_defaults_to_false(mock_webdriver, chrome_os, devtools):
    b, drv = _running_backend(mock_webdriver)
    assert b.new_blank_tab().incognito is False
    assert drv.switch_to.new_window.called


# --- opening an incognito tab ----------------------------------------------

@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_incognito_tab_is_a_target_in_a_new_browser_context(mock_webdriver, chrome_os, devtools):
    b, drv = _running_backend(mock_webdriver)

    tab = b.new_blank_tab(incognito=True)

    assert devtools.methods() == ["Target.createBrowserContext", "Target.createTarget"]
    # The target goes in the context that was just created — not the default one,
    # which would be an ordinary tab wearing the label.
    _, params = devtools.calls[1]
    assert params["browserContextId"] == "CTX1"
    assert params["url"] == "about:blank"
    # The handle selenium drives is the CDP target id.
    drv.switch_to.window.assert_called_with("TGT1")
    assert tab.handle == "TGT1"
    assert tab.incognito is True
    assert tab.selected is True
    # Never a normal tab as well: that would leave a stray window behind.
    assert not drv.switch_to.new_window.called


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_the_socket_is_opened_with_the_allowed_origin_and_closed(mock_webdriver, chrome_os, devtools):
    # _chrome_args pins --remote-allow-origins to this exact loopback origin;
    # anything else and Chrome >= 111 rejects the handshake.
    b, _ = _running_backend(mock_webdriver)

    b.new_blank_tab(incognito=True)

    assert devtools.origins == [f"http://127.0.0.1:{devtools.port}"] * 2
    assert all(sock.closed for sock in devtools.sockets)   # no socket left open


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_a_sessions_incognito_tabs_share_one_context(mock_webdriver, chrome_os, devtools):
    # Two incognito windows in a real Chrome share a cookie jar; two incognito
    # tabs here must too, or "log in once, then browse" would not work.
    b, _ = _running_backend(mock_webdriver)

    first = b.new_blank_tab(incognito=True)
    second = b.new_blank_tab(incognito=True)

    assert devtools.methods() == [
        "Target.createBrowserContext", "Target.createTarget", "Target.createTarget"]
    assert devtools.calls[2][1]["browserContextId"] == "CTX1"   # the same context
    assert first.handle != second.handle


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_list_tabs_reports_which_tabs_are_incognito(mock_webdriver, chrome_os, devtools):
    b, drv = _running_backend(mock_webdriver)
    b.new_blank_tab(incognito=True)
    drv.window_handles = ["h1", "TGT1"]

    by_handle = {tab.handle: tab.incognito for tab in b.list_tabs()}

    assert by_handle == {"h1": False, "TGT1": True}


def test_the_wire_shape_carries_incognito():
    from browden.common.tab import TabInfo
    wire = TabInfo(handle="h", url="u", title="t", selected=True,
                   profile_dir="/p", incognito=True).as_dict(id="ns-h")
    assert wire["incognito"] is True
    # and it is a real JSON boolean, never a stringified one
    assert isinstance(wire["incognito"], bool)


# --- the context's lifetime -------------------------------------------------

@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_closing_the_last_incognito_tab_disposes_the_context(mock_webdriver, chrome_os, devtools):
    # Chrome ends an incognito session when its last window closes, discarding
    # the cookies. If browden kept the context alive, a later incognito tab
    # would silently inherit the earlier one's logins.
    b, drv = _running_backend(mock_webdriver)
    b.new_blank_tab(incognito=True)
    drv.window_handles = ["h1", "TGT1"]

    b.close_tab("TGT1")

    assert devtools.methods()[-1] == "Target.disposeBrowserContext"
    assert devtools.calls[-1][1] == {"browserContextId": "CTX1"}
    # ... and the next incognito tab gets a genuinely fresh context
    b.new_blank_tab(incognito=True)
    assert devtools.calls[-1][1]["browserContextId"] == "CTX2"


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_closing_one_of_two_incognito_tabs_keeps_the_context(mock_webdriver, chrome_os, devtools):
    b, drv = _running_backend(mock_webdriver)
    b.new_blank_tab(incognito=True)
    b.new_blank_tab(incognito=True)
    drv.window_handles = ["h1", "TGT1", "TGT2"]

    b.close_tab("TGT1")

    assert "Target.disposeBrowserContext" not in devtools.methods()


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_closing_a_normal_tab_never_disposes_the_context(mock_webdriver, chrome_os, devtools):
    b, drv = _running_backend(mock_webdriver, handles=("h1", "h2"))
    b.new_blank_tab(incognito=True)
    drv.window_handles = ["h1", "h2", "TGT1"]

    b.close_tab("h2")

    assert "Target.disposeBrowserContext" not in devtools.methods()


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_a_failed_dispose_is_warned_and_swallowed(mock_webdriver, chrome_os, devtools, monkeypatch):
    # The context dies with the browser regardless, so a failed dispose costs
    # memory until shutdown — it must never fail the close_tab the agent asked for.
    b, drv = _running_backend(mock_webdriver)
    b.new_blank_tab(incognito=True)
    drv.window_handles = ["h1", "TGT1"]
    devtools.dispose_error = {"code": -32000, "message": "Not allowed"}
    log = MagicMock()
    monkeypatch.setattr(backend, "logger", log)

    b.close_tab("TGT1")          # must not raise

    log.warning.assert_called_once()
    assert "incognito" in log.warning.call_args[0][0]


@patch("browden.web_navigator.selenium_chrome.backend.webdriver")
def test_a_dead_browser_forgets_the_context(mock_webdriver, chrome_os, devtools):
    # The context lived inside that Chrome. Reusing its id against the next
    # launch's port would address a context that no longer exists.
    b, _ = _running_backend(mock_webdriver)
    b.new_blank_tab(incognito=True)
    assert b._incognito_context == "CTX1"

    b._teardown()

    assert b._incognito_context is None
    assert b._incognito_handles == set()
    assert b._devtools_port is None


# --- cdp.py's own error handling -------------------------------------------

def test_cdp_errors_are_typed_not_raw(monkeypatch):
    # A wedged or unreachable DevTools endpoint must surface as CdpError, so the
    # tool layer reports it instead of leaking a urllib/websocket traceback.
    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(cdp.urllib.request, "urlopen", refuse)
    with pytest.raises(cdp.CdpError, match="DevTools endpoint"):
        cdp.create_browser_context(9222)


def test_cdp_surfaces_a_protocol_error(monkeypatch, devtools):
    devtools.dispose_error = {"code": -32000, "message": "No such context"}
    with pytest.raises(cdp.CdpError, match="No such context"):
        cdp.dispose_browser_context(devtools.port, "CTXZ")
