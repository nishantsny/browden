"""Shared harness for the gate-atomicity tests (reads and writes).

A real ``BrowserSessionManager`` — so the real FIFO driver lock — over a fake
backend with one tab (``h1``) whose document is ``pages[url]``. Any backend
call can be *parked*: it signals ``parked`` and blocks, holding the driver lock
as a slow real call would, until the test sets ``resume``. That makes an
interleaving deterministic rather than timing-dependent.
"""
import asyncio
import threading
import time
from unittest.mock import patch

from browden.common.tab import TabInfo
from browden.configs.loader import RuntimeConfigurationRefresher
from browden.mcp.session_management.browser_session_manager import BrowserSessionManager

TAB = "ns-h1"
PROFILE = "/fake/profile"


class OnePageBackend:
    """One tab whose document is ``pages[url]``.

    ``redirects`` maps a URL to where loading it actually lands — on
    ``navigate`` and on ``reload`` alike, the way a server-side 302 does.
    ``reads`` records ``(url, what)`` for every time page content left the
    browser (HTML fetched, screenshot taken); ``actions`` records every write as
    ``(url it landed on, action, selector)``.
    """

    def __init__(self, url: str, pages: dict, redirects: dict | None = None):
        self.url = url
        self.pages = pages
        self.redirects = redirects or {}
        self.reads: list[tuple[str, str]] = []
        self.actions: list[tuple[str, str, str]] = []
        self.closed: list[str] = []
        self.parked = threading.Event()
        self.resume = threading.Event()
        self._park: str | None = None

    # -- parking --------------------------------------------------------------

    def park_next(self, method: str) -> None:
        """Park the next call to ``method`` (after it has done its work)."""
        self._park = method

    def _maybe_park(self, method: str) -> None:
        if self._park == method:
            self._park = None
            self.parked.set()
            assert self.resume.wait(5), f"test never resumed the parked {method}"

    # -- backend interface ----------------------------------------------------

    def get_profile_dir(self):
        return PROFILE

    def is_running(self):
        return True

    def select_tab(self, handle):
        assert handle == "h1"

    def document_url(self):
        url = self.url
        self._maybe_park("document_url")
        return url

    def current_url(self):
        return self.url

    def _tab(self):
        return TabInfo(handle="h1", url=self.url, title="t", selected=True, profile_dir=PROFILE)

    def list_tabs(self):
        return [self._tab()]

    def list_handles(self):
        return ["h1"]

    def close_tab(self, handle):
        self.closed.append(handle)

    def get_tab_html(self):
        self.reads.append((self.url, "html"))
        html = self.pages.get(self.url, "<html></html>")
        self._maybe_park("get_tab_html")
        return html

    def screenshot(self):
        self.reads.append((self.url, "screenshot"))
        self._maybe_park("screenshot")
        return b"\x89PNG " + self.url.encode()

    def navigate(self, url):
        self.url = self.redirects.get(url, url)
        self._maybe_park("navigate")
        return self._tab()

    def reload(self):
        self.url = self.redirects.get(self.url, self.url)
        self._maybe_park("reload")
        return self._tab()

    def _act(self, action, css_selector):
        self.actions.append((self.url, action, css_selector))
        return {"url": self.url, "title": "t"}

    def click_element(self, css_selector):
        return {"clicked": True, **self._act("click", css_selector)}

    def insert_text_element(self, css_selector, value):
        return {"inserted": True, "value": value, **self._act("insert_text", css_selector)}

    def press_key_element(self, css_selector, key):
        return {"pressed": key, **self._act("press_key", css_selector)}


def make_session(backend, clock=time.monotonic):
    return BrowserSessionManager(backend, namespace="ns", clock=clock, start_reaper=False)


async def until_queued(session, n=1):
    """Yield until ``n`` coroutines are waiting on the session's driver lock."""
    for _ in range(500):
        if len(session._lock._waiters or ()) >= n:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("request never queued on the driver lock")


def serve(server, session, configuration):
    """Context managers routing every id to ``session`` and gating with ``configuration``."""
    return (patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(configuration)),
            patch.object(server._store, "route", return_value=session),
            patch.object(server._store, "sessions", return_value=[session]))
