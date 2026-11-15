"""E2e: an incognito tab really is isolated, against a real Chrome and real cookies.

The unit tests pin the CDP calls and the context's bookkeeping. What they cannot
show is the only thing the feature is for: that a tab opened with
``incognito=True`` does not carry the profile's cookies, that the profile does
not pick up the incognito tab's, and that closing the last incognito tab
actually discards them.

Cookies are set and read over HTTP by a tiny local server — ``Set-Cookie`` on the
way out, the received ``Cookie`` header echoed into the page on the way back — so
the assertion is about what Chrome actually sent, not about what JavaScript
reported. Everything is served from ``127.0.0.1``, so the run is local and
offline.
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.web_navigator.utils.network_utils import get_free_port
from gates import OPEN_READ_GATE

sys.path.insert(0, os.path.dirname(__file__))


class _Handler(BaseHTTPRequestHandler):
    """``/set?v=x`` sets ``probe=x``; any other path echoes the cookies received."""

    def do_GET(self):
        if self.path.startswith("/set"):
            value = self.path.partition("v=")[2] or "unset"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            # Session cookie (no Max-Age): the kind a login uses, and the kind an
            # incognito context is meant to drop when it ends.
            self.send_header("Set-Cookie", f"probe={value}; Path=/")
            self.end_headers()
            self.wfile.write(b"<html><body><div id='state'>set</div></body></html>")
            return
        seen = self.headers.get("Cookie") or "none"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            f"<html><body><div id='state'>{seen}</div></body></html>".encode())

    def log_message(self, *args):          # keep the test output quiet
        pass


@pytest.fixture
def site():
    """A local HTTP server that sets and echoes cookies; yields its base URL."""
    port = get_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join()


@pytest.fixture
def session(new_backend, tmp_path):
    # A fresh profile per test: this test is about what a profile does and does
    # not carry, so it must not start with cookies from another run.
    backend = new_backend(tmp_path / "profile")
    return BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


async def _open(session, *, incognito=False):
    return await session.new_blank_tab(max_tabs=10, incognito=incognito)


async def _goto(session, tab, url):
    return await session.navigate(url, id=tab["id"], gate=OPEN_READ_GATE)


async def _cookies_sent(session, tab, site):
    """What Chrome sent as the ``Cookie`` header for ``tab``, as the server saw it."""
    await _goto(session, tab, f"{site}/show")
    state = await session.query_selector("#state", id=tab["id"], gate=OPEN_READ_GATE)
    assert state["found"] is True
    return state["element"]["text"]


@pytest.mark.asyncio
async def test_an_incognito_tab_does_not_carry_the_profiles_cookies(session, site):
    normal = await _open(session)
    await _goto(session, normal, f"{site}/set?v=profile")
    assert "probe=profile" in await _cookies_sent(session, normal, site)

    incognito = await _open(session, incognito=True)

    assert incognito["incognito"] is True
    # The whole point: this tab arrives signed out.
    assert await _cookies_sent(session, incognito, site) == "none"


@pytest.mark.asyncio
async def test_the_profile_does_not_pick_up_an_incognito_cookie(session, site):
    normal = await _open(session)
    await _goto(session, normal, f"{site}/set?v=profile")
    incognito = await _open(session, incognito=True)
    await _goto(session, incognito, f"{site}/set?v=incognito")

    assert "probe=incognito" in await _cookies_sent(session, incognito, site)
    # Isolation is two-way: logging into a second account in an incognito tab
    # must not disturb the profile's own session.
    profile_sees = await _cookies_sent(session, normal, site)
    assert "probe=profile" in profile_sees
    assert "incognito" not in profile_sees


@pytest.mark.asyncio
async def test_two_incognito_tabs_share_one_session(session, site):
    # Two incognito windows in a real Chrome share a cookie jar, so a login in
    # one is usable in the other. Same here.
    first = await _open(session, incognito=True)
    await _goto(session, first, f"{site}/set?v=shared")

    second = await _open(session, incognito=True)

    assert second["incognito"] is True
    assert "probe=shared" in await _cookies_sent(session, second, site)


@pytest.mark.asyncio
async def test_closing_the_last_incognito_tab_discards_its_cookies(session, site):
    incognito = await _open(session, incognito=True)
    await _goto(session, incognito, f"{site}/set?v=temporary")
    assert "probe=temporary" in await _cookies_sent(session, incognito, site)

    await session.close_tab(id=incognito["id"])

    # A later incognito tab is a genuinely new session, not a continuation.
    reopened = await _open(session, incognito=True)
    assert await _cookies_sent(session, reopened, site) == "none"


@pytest.mark.asyncio
async def test_a_normal_tab_is_the_default_and_keeps_its_cookies(session, site):
    # The flag is opt-in; nothing changes for callers that never pass it.
    first = await _open(session)
    assert first["incognito"] is False
    await _goto(session, first, f"{site}/set?v=persisted")

    second = await _open(session)

    assert second["incognito"] is False
    assert "probe=persisted" in await _cookies_sent(session, second, site)


@pytest.mark.asyncio
async def test_list_tabs_says_which_tabs_are_incognito(session, site):
    normal = await _open(session)
    incognito = await _open(session, incognito=True)

    listed = {tab["id"]: tab["incognito"]
              for tab in await session.list_tabs(gate=OPEN_READ_GATE)}

    assert listed[normal["id"]] is False
    assert listed[incognito["id"]] is True


@pytest.mark.asyncio
async def test_an_incognito_tab_is_still_governed_by_the_profiles_read_gate(session, site):
    """Incognito changes storage, not permission.

    The tab stays in the same profile and session, so the profile's rules decide
    where it may go. If incognito had been implemented by quietly routing the tab
    to some other profile directory, it would have escaped them — this is the
    test that would have caught it.
    """
    refused = []

    def check_page(url):
        if "/forbidden" in url:
            refused.append(url)
            raise ValueError(f"blocked by policy: {url}")

    from browden.mcp.validator import ReadGate
    gate = ReadGate(check_page=check_page)

    incognito = await _open(session, incognito=True)

    with pytest.raises(ValueError, match="blocked by policy"):
        await session.navigate(f"{site}/forbidden", id=incognito["id"], gate=gate)
    assert refused, "the gate was never consulted for the incognito tab"

    # ... and an admitted page still loads, so the gate is being applied rather
    # than the tab simply being broken.
    allowed = await session.navigate(f"{site}/show", id=incognito["id"], gate=gate)
    assert allowed["id"] == incognito["id"]
