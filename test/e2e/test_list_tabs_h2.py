"""End-to-end: ``list_tabs`` closes off-list tabs (H2) in the listing's own hold.

``BrowserSessionManager.list_tabs`` lists a profile's tabs, gates each tab's URL
with the ``ReadGate`` it is handed and closes the ones the gate refuses — all in
one driver hold. Each branch of that close is pinned here against real Chrome:

* an off-list tab is closed in Chrome and left out of the listing;
* the LAST tab can't be closed (the backend's last-tab guard): it stays open,
  and is still never listed;
* a tab that vanished between the listing and its close counts as closed — this
  relies on real Selenium raising ``NoSuchWindowException`` for a gone handle,
  which the backend turns into ``TabNotFoundError``.

The first group drives the session over a real backend (``data:`` pages, a gate
built for the test). The second drives a real MCP server: a tab is left on a
page the rules allow, the watched allowlist is then tightened (hot reload), and
the very next ``list_tabs`` must close it — the way a tab ends up off-list in
production.
"""
import asyncio
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from browden.configs.loader.refresher import DEFAULT_RELOAD_INTERVAL_SECONDS
from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.mcp.validator import ReadGate, ValidationError
from browden.web_navigator.utils.network_utils import get_free_port
from gates import OPEN_READ_GATE

ON = "data:text/html," + urllib.parse.quote("<p>on-list</p>")
OFF = "data:text/html," + urllib.parse.quote("<p>off-list</p>")


def _refuse_off(url: str) -> None:
    if "off-list" in urllib.parse.unquote(url):
        raise ValidationError(f"URL not on the read allowlist: {url}")


REFUSE_OFF = ReadGate(check_page=_refuse_off)


# -- the session over real Chrome ------------------------------------------------

@pytest.fixture
def chrome(new_backend, tmp_path):
    backend = new_backend(tmp_path / "profile")
    return backend, BrowserSessionManager(backend, namespace="e2e", start_reaper=False)


async def _open(session, url):
    tab = await session.new_blank_tab(max_tabs=10)
    return await session.navigate(url, id=tab["id"], gate=OPEN_READ_GATE)


def _handle(tab_id: str) -> str:
    return tab_id.split("-", 1)[1]


@pytest.mark.asyncio
async def test_an_off_list_tab_is_closed_in_chrome_and_not_listed(chrome):
    backend, session = chrome
    kept = await _open(session, ON)
    off = await _open(session, OFF)

    listed = [t["id"] for t in await session.list_tabs(gate=REFUSE_OFF)]

    assert kept["id"] in listed and off["id"] not in listed
    assert _handle(off["id"]) not in backend.list_handles(), "the off-list tab is still open in Chrome"
    assert _handle(off["id"]) not in session._registry._last_access


@pytest.mark.asyncio
async def test_the_last_tab_cannot_be_closed_but_is_never_listed(chrome):
    backend, session = chrome
    off = await _open(session, OFF)
    for handle in backend.list_handles():  # leave the off-list tab as the only one
        if handle != _handle(off["id"]):
            await session.close_tab(f"e2e-{handle}")
    assert backend.list_handles() == [_handle(off["id"])]

    listed = await session.list_tabs(gate=REFUSE_OFF)

    assert listed == []
    assert backend.list_handles() == [_handle(off["id"])], "the last tab must stay open"
    assert _handle(off["id"]) in session._registry._last_access  # still open, so still tracked


@pytest.mark.asyncio
async def test_a_tab_gone_before_its_close_counts_as_closed(chrome):
    backend, session = chrome
    kept = await _open(session, ON)
    off = await _open(session, OFF)
    gone = _handle(off["id"])

    def refuse_and_vanish(url):
        # The tab disappears after it was listed and before list_tabs closes it —
        # as if the human closed it in Chrome mid-hold. The close that follows
        # must hit Selenium's real missing-window error and treat it as closed.
        if "off-list" in urllib.parse.unquote(url):
            backend.close_tab(gone)
            raise ValidationError(f"URL not on the read allowlist: {url}")

    listed = [t["id"] for t in await session.list_tabs(gate=ReadGate(check_page=refuse_and_vanish))]

    assert kept["id"] in listed and off["id"] not in listed
    assert gone not in backend.list_handles()
    assert gone not in session._registry._last_access, "an already-gone tab must not stay tracked"


# -- through the MCP server: the rules tighten under an open tab ----------------

_PAGE = b"<html><body><p id='p'>page</p></body></html>"
_RELOAD_DEADLINE = DEFAULT_RELOAD_INTERVAL_SECONDS + 15


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    """A local HTTP server on 127.0.0.1 that 200s every path; yields its base URL."""
    port = get_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join()


def _rules(*paths):
    return {"read": {"enabled": True, "tranco": {"enabled": False},
                     "website_overrides": {"127.0.0.1": [{"path": list(paths)}]}}}


@pytest.fixture
def server(tmp_path):
    """A live MCP server watching an allowlist that starts by allowing /ok and /off."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    cfg = tmp_path / "allowlist.yaml"
    cfg.write_text(yaml.safe_dump(_rules("^/ok", "^/off"), sort_keys=False))
    cache = tmp_path / "cache"
    cache.mkdir()
    harness = McpServerHarness(cache, allowlist_path=cfg)
    harness.start()
    yield harness
    harness.stop()


def _json(res):
    out = []
    for c in res.content or []:
        if getattr(c, "type", None) == "text":
            data = json.loads(c.text)
            out.extend(data if isinstance(data, list) else [data])
    return out


async def _listed_ids(mcp) -> list[str]:
    return [t["id"] for t in _json(await mcp.call_tool("list_tabs", {}))]


async def _tab_on(mcp, url) -> str:
    tab = _json(await mcp.call_tool("new_blank_tab", {}))[0]["id"]
    await mcp.call_tool("navigate", {"url": url, "id": tab})
    return tab


async def _tighten_and_wait_until_unlisted(mcp, server, tab) -> None:
    """Drop /off from the watched rules, then poll list_tabs until ``tab`` is gone from it."""
    server.allowlist_path.write_text(yaml.safe_dump(_rules("^/ok"), sort_keys=False))
    end = time.monotonic() + _RELOAD_DEADLINE
    while time.monotonic() < end:
        if tab not in await _listed_ids(mcp):
            return
        await asyncio.sleep(0.5)
    raise AssertionError("the tightened rules never took effect on list_tabs")


@pytest.mark.asyncio
async def test_mcp_list_tabs_closes_a_tab_the_rules_stopped_allowing(server, mcp_client_session, site):
    async with mcp_client_session(server) as mcp:
        ok = await _tab_on(mcp, f"{site}/ok")
        off = await _tab_on(mcp, f"{site}/off")
        assert {ok, off} <= set(await _listed_ids(mcp))

        await _tighten_and_wait_until_unlisted(mcp, server, off)

        assert ok in await _listed_ids(mcp)
        # Closed, not just hidden: the tab is gone.
        gone = _json(await mcp.call_tool("select_tab", {"id": off}))[0]
        assert "no longer open" in gone.get("error", ""), gone


@pytest.mark.asyncio
async def test_mcp_list_tabs_hides_the_last_tab_it_cannot_close(server, mcp_client_session, site):
    async with mcp_client_session(server) as mcp:
        off = await _tab_on(mcp, f"{site}/off")
        for other in await _listed_ids(mcp):  # leave the off tab as the profile's only one
            if other != off:
                await mcp.call_tool("close_tab", {"id": other})
        assert await _listed_ids(mcp) == [off]

        await _tighten_and_wait_until_unlisted(mcp, server, off)

        # It could not be closed (last tab), so it is still open...
        selected = _json(await mcp.call_tool("select_tab", {"id": off}))[0]
        assert selected.get("selected") == off, selected
        # ...but it stays unlisted, and unreadable.
        assert await _listed_ids(mcp) == []
        try:
            res = await mcp.call_tool("query_selector", {"css_selector": "#p", "id": off})
            refused = "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")
        except Exception as e:  # the SDK may raise on a server-side ValidationError
            refused = str(e)
        assert "read allowlist" in refused, refused
