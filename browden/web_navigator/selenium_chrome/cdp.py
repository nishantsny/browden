"""The browser-level CDP calls selenium cannot make, for incognito tabs.

chromedriver attaches its CDP session to a *page* target, so browser-scoped
commands are refused through it: ``driver.execute_cdp_cmd(
"Target.createBrowserContext")`` comes back
``{"code": -32000, "message": "Not allowed"}``. An incognito browser context
has to be asked for on the browser-level socket — the one ``/json/version``
advertises — so this module speaks just enough CDP to open one.

Deliberately tiny and stateless: connect, send one or two commands, close. No
background thread, no event pump, no long-lived socket to keep healthy
alongside the one chromedriver already owns. Incognito tabs are opened rarely
(once per ``new_blank_tab(incognito=True)``), so a short connection per call is
cheaper to reason about than a connection to maintain — and it cannot drift out
of sync with the driver's own view of the browser.

Once the target exists, selenium drives it like any other tab: chromedriver
lists targets from other browser contexts in ``window_handles``, and
``switch_to.window(target_id)`` works. Nothing else in browden needs to know
these tabs are special, beyond reporting it.
"""
import json
import urllib.request

from ...dependencies.websocket import create_connection

# A local socket to a process we launched: every call is a round trip to
# 127.0.0.1 answered by Chrome's browser process without touching the network or
# the page. Generous enough for a loaded host, short enough that a wedged
# browser surfaces as an error instead of hanging a tool call.
CDP_TIMEOUT_SECONDS = 10.0


class CdpError(RuntimeError):
    """A browser-level CDP call failed, or the socket could not be opened."""


def _browser_ws_url(port: int, timeout: float) -> str:
    """The browser-level DevTools websocket URL for the Chrome on ``port``."""
    try:
        with urllib.request.urlopen(          # noqa: S310 - fixed loopback scheme
                f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            return json.loads(resp.read())["webSocketDebuggerUrl"]
    except Exception as e:
        raise CdpError(f"could not read the DevTools endpoint on port {port}: {e}") from e


class _Socket:
    """One short-lived browser-level CDP connection."""

    def __init__(self, port: int, timeout: float):
        url = _browser_ws_url(port, timeout)
        try:
            # Chrome >= 111 checks the Origin header against
            # --remote-allow-origins, which _chrome_args pins to this exact
            # loopback origin. Send that, or the handshake is rejected.
            self._ws = create_connection(
                url, origin=f"http://127.0.0.1:{port}", timeout=timeout)
        except Exception as e:
            raise CdpError(f"could not open the DevTools socket on port {port}: {e}") from e
        self._next_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        want = self._next_id
        try:
            self._ws.send(json.dumps({"id": want, "method": method, "params": params or {}}))
            # The browser socket also carries unsolicited events; skip anything
            # that is not the reply to the call just sent.
            while True:
                message = json.loads(self._ws.recv())
                if message.get("id") != want:
                    continue
                if "error" in message:
                    raise CdpError(f"{method} failed: {message['error']}")
                return message.get("result", {})
        except CdpError:
            raise
        except Exception as e:
            raise CdpError(f"{method} failed: {e}") from e

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass          # best-effort: the caller already has its answer


def create_browser_context(port: int, timeout: float = CDP_TIMEOUT_SECONDS) -> str:
    """Create an incognito browser context and return its id.

    The context is where the isolation lives: it has its own cookie jar and
    storage, keeps nothing on disk, and cannot see the profile's cookies (nor
    can the profile see its).
    """
    sock = _Socket(port, timeout)
    try:
        return sock.call("Target.createBrowserContext")["browserContextId"]
    finally:
        sock.close()


def create_target(port: int, browser_context_id: str,
                  timeout: float = CDP_TIMEOUT_SECONDS) -> str:
    """Open a blank page target inside ``browser_context_id``; return its id.

    The target id is also the selenium window handle for that tab.
    """
    sock = _Socket(port, timeout)
    try:
        return sock.call("Target.createTarget",
                         {"url": "about:blank",
                          "browserContextId": browser_context_id})["targetId"]
    finally:
        sock.close()


def dispose_browser_context(port: int, browser_context_id: str,
                            timeout: float = CDP_TIMEOUT_SECONDS) -> None:
    """Discard an incognito context and everything in it. Best-effort.

    Called when the last incognito tab closes, which is where Chrome itself ends
    an incognito session. Failure is logged by the caller and otherwise ignored:
    the context dies with the browser regardless, so a failed dispose costs
    memory until shutdown, never correctness.
    """
    sock = _Socket(port, timeout)
    try:
        sock.call("Target.disposeBrowserContext",
                  {"browserContextId": browser_context_id})
    finally:
        sock.close()
