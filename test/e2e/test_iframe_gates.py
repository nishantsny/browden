"""End-to-end: the frame tools' gates, through the real MCP server and Chrome.

``test_iframe_inspection.py`` drives ``BrowserSessionManager`` directly, so it
proves the focus mechanics but never runs a gate. These go through the server
tools, so every verdict below comes from the same gates a client hits:

* **same-origin frame** — entered, read, and left again;
* **cross-origin frame** — refused after the switch, and focus is back on top;
* **declared src off the read allowlist** — refused, focus stays on top;
* **ascent re-gate** — a middle frame navigated cross-origin while we were
  deeper is refused on ``switch_to_parent_frame``, which retreats to the top:
  by the same-origin check where the new origin is readable, and by the read
  check (which also refuses reads while still deeper) where it isn't;
* **writes inside a frame** — judged by the *frame's* URL, not the top page's;
* **profile scoping** — a frame readable in one profile only;
* **a vanished frame** — the next read errors once, then the tab is at its top;
* **inherited-origin frames** — ``srcdoc`` and script-written ``about:blank``
  frames are judged by the page that wrote them; a sandboxed one (opaque
  origin) is refused.

The site is served on ``127.0.0.1`` and reached as ``localhost`` for the second
origin; both are opted in for plain http by being named in an override, so
every load is local and offline.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browden.web_navigator.utils.network_utils import get_free_port

_INNER = (
    "<div id='in-frame'>INSIDE</div>"
    "<button id='btn' onclick=\"document.getElementById('in-frame').textContent='CLICKED'\">"
    "Inner button</button>"
    "<button id='mover' onclick=\"parent.location.href='{other}/frames/landing'\">"
    "Move parent</button>"
    "<button id='remover' onclick=\"parent.document.getElementById('child').remove()\">"
    "Remove me</button>"
)
_TOP = "<div id='top-only'>TOP</div>"
_SRCDOC_BODY = "&lt;div id='in-frame'&gt;INSIDE&lt;/div&gt;"


def _pages(other: str) -> "dict[str, str]":
    """Path -> HTML body. ``other`` is the second origin (``localhost``)."""
    return {
        "/frames/inner": _INNER.format(other=other),
        "/frames/same": _TOP + "<button id='topbtn'>Inner button</button>"
                               "<iframe id='child' src='/frames/inner'></iframe>",
        "/frames/cross": _TOP + f"<iframe id='child' src='{other}/frames/inner'></iframe>",
        "/frames/scoped": _TOP + "<iframe id='child' src='/private/inner'></iframe>",
        "/private/inner": "<div id='in-frame'>PRIVATE</div>",
        "/frames/nested": _TOP + "<iframe id='mid' src='/frames/middle'></iframe>",
        "/frames/middle": "<div id='middle-only'>MIDDLE</div>"
                          "<iframe id='child' src='/frames/inner'></iframe>",
        # Served on the second origin once the middle frame is moved there; it
        # has a #child too, so the recorded frame path still resolves into it.
        "/frames/landing": "<div id='landing'>LANDING</div>"
                           "<iframe id='child' src='/frames/inner'></iframe>",
        "/frames/srcdoc": _TOP + f'<iframe id="child" srcdoc="{_SRCDOC_BODY}"></iframe>',
        "/frames/blank": _TOP + "<iframe id='child'></iframe><script>"
                                "document.getElementById('child').contentDocument.body"
                                ".innerHTML = \"<div id='in-frame'>INSIDE</div>\";</script>",
        "/frames/sandboxed": _TOP + f'<iframe id="child" sandbox srcdoc="{_SRCDOC_BODY}">'
                                    "</iframe>",
    }


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        other = f"http://localhost:{self.server.server_port}"
        body = _pages(other).get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture
def site():
    """The frame pages on 127.0.0.1; yields the base URL (``localhost`` is the
    same server under a second origin)."""
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
def profiles(tmp_path):
    """``plain`` gets only the global rules; ``wide`` can also read /private/*
    and the whole second origin."""
    return tuple((tmp_path / f"{name}-profile").resolve() for name in ("plain", "wide"))


def _config(plain, wide) -> str:
    return (
        "read:\n"
        "  enabled: true\n"
        "  tranco: {enabled: false}\n"
        "  website_overrides:\n"
        "    127.0.0.1: ['^/frames/.*']\n"
        # Clicks only on the frame's own page, so a click is authorized exactly
        # when the gate judges it by the frame's URL rather than the top page's.
        "click:\n"
        "  127.0.0.1:\n"
        "    - path: ['^/frames/inner$']\n"
        "      label: '(?i)(inner button|move parent|remove me)'\n"
        "profiles:\n"
        f"  {wide}:\n"
        "    read:\n"
        "      website_overrides:\n"
        "        127.0.0.1: ['^/private/.*']\n"
        "        localhost: ['.*']\n"
    )


@pytest.fixture
def frame_server(tmp_path, profiles):
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(_config(*profiles))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    harness = McpServerHarness(cache_dir, allowlist_path=allowlist)
    harness.start()
    yield harness
    harness.stop()


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


async def _call_text(mcp, tool: str, args: dict) -> str:
    """A tool's result text: the JSON result, or the refusal message (the SDK
    surfaces a server-side ValidationError as an error result or a raise)."""
    try:
        return _text(await mcp.call_tool(tool, args))
    except Exception as e:  # noqa: BLE001 — the SDK may raise on a ValidationError
        return str(e)


async def _open(mcp, profile_dir, url: str) -> str:
    res = await mcp.call_tool("new_blank_tab", {"profile_dir": str(profile_dir)})
    tab = json.loads(res.content[0].text)["id"]
    landed = await _call_text(mcp, "navigate", {"url": url, "id": tab})
    assert "allowlist" not in landed.lower(), landed
    return tab


async def _find(mcp, tab: str, selector: str) -> dict:
    return json.loads(await _call_text(mcp, "query_selector",
                                       {"css_selector": selector, "id": tab}))


async def _switch(mcp, tab: str, selector: str) -> str:
    return await _call_text(mcp, "switch_to_frame", {"css_selector": selector, "id": tab})


async def _assert_at_top(mcp, tab: str) -> None:
    assert (await _find(mcp, tab, "#top-only"))["found"] is True
    assert (await _find(mcp, tab, "#in-frame"))["found"] is False


# -- entry: same-origin in, cross-origin and unreadable frames refused --------

@pytest.mark.asyncio
async def test_same_origin_frame_is_entered_read_and_left(
        frame_server, mcp_client_session, site, profiles):
    plain = profiles[0]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, plain, f"{site}/frames/same")

        entered = json.loads(await _switch(mcp, tab, "#child"))
        assert entered["frame_url"] == f"{site}/frames/inner"
        found = await _find(mcp, tab, "#in-frame")
        assert found["found"] is True and found["element"]["text"] == "INSIDE"
        assert (await _find(mcp, tab, "#top-only"))["found"] is False

        await _call_text(mcp, "switch_to_default_content", {"id": tab})
        await _assert_at_top(mcp, tab)


@pytest.mark.asyncio
async def test_cross_origin_frame_is_refused_and_focus_returns_to_top(
        frame_server, mcp_client_session, site, profiles):
    # The wide profile may read the second origin, so only the same-origin
    # check can refuse this frame.
    wide = profiles[1]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, wide, f"{site}/frames/cross")

        assert "cross-origin" in (await _switch(mcp, tab, "#child")).lower()
        await _assert_at_top(mcp, tab)


@pytest.mark.asyncio
async def test_frame_whose_src_is_not_readable_is_refused(
        frame_server, mcp_client_session, site, profiles):
    # The plain profile can't read the second origin at all: the declared src
    # fails the read gate, which is a refusal on the allowlist, not on origin.
    plain = profiles[0]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, plain, f"{site}/frames/cross")

        refused = (await _switch(mcp, tab, "#child")).lower()
        assert "frame_url" not in refused
        assert "cross-origin" not in refused
        await _assert_at_top(mcp, tab)


@pytest.mark.asyncio
async def test_a_frame_is_judged_by_the_tabs_own_profile(
        frame_server, mcp_client_session, site, profiles):
    plain, wide = profiles
    async with mcp_client_session(frame_server) as mcp:
        plain_tab = await _open(mcp, plain, f"{site}/frames/scoped")
        wide_tab = await _open(mcp, wide, f"{site}/frames/scoped")

        # /private/* is readable only in the wide profile.
        assert "frame_url" not in await _switch(mcp, plain_tab, "#child")
        await _assert_at_top(mcp, plain_tab)

        json.loads(await _switch(mcp, wide_tab, "#child"))
        assert (await _find(mcp, wide_tab, "#in-frame"))["element"]["text"] == "PRIVATE"


# -- ascent: the landed ancestor is re-gated ----------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("profile, readable", [("wide", True), ("plain", False)])
async def test_parent_moved_cross_origin_is_refused_on_ascent(
        frame_server, mcp_client_session, site, profiles, profile, readable):
    # wide: the second origin is readable, so only the same-origin check can
    # refuse the landed parent. plain: it isn't readable either, so the read
    # check refuses it first, and reads while still deeper are refused too.
    profile_dir = dict(zip(("plain", "wide"), profiles))[profile]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, profile_dir, f"{site}/frames/nested")
        json.loads(await _switch(mcp, tab, "#mid"))
        json.loads(await _switch(mcp, tab, "#child"))

        # From the innermost frame, move its parent to the second origin. The
        # landing page has a #child too, so the recorded path still resolves.
        assert "clicked" in await _call_text(
            mcp, "click", {"css_selector": "#mover", "id": tab})
        await asyncio.sleep(2)

        # Focus is now in the landing page's #child, on the second origin.
        deeper = await _call_text(mcp, "query_selector",
                                  {"css_selector": "#in-frame", "id": tab})
        assert ('"found"' in deeper) is readable, deeper

        refused = (await _call_text(mcp, "switch_to_parent_frame", {"id": tab})).lower()
        assert "frame_url" not in refused
        assert ("cross-origin" in refused) is readable, refused
        await _assert_at_top(mcp, tab)


# -- writes inside a frame ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_click_inside_a_frame_is_judged_by_the_frames_url(
        frame_server, mcp_client_session, site, profiles):
    plain = profiles[0]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, plain, f"{site}/frames/same")

        # The click rule covers /frames/inner only: the same label on the top
        # page (/frames/same) is refused...
        assert "clicked" not in await _call_text(
            mcp, "click", {"css_selector": "#topbtn", "id": tab})

        # ...and inside the frame, whose own URL is /frames/inner, it is allowed.
        json.loads(await _switch(mcp, tab, "#child"))
        assert "clicked" in await _call_text(
            mcp, "click", {"css_selector": "#btn", "id": tab})
        assert (await _find(mcp, tab, "#in-frame"))["element"]["text"] == "CLICKED"


@pytest.mark.asyncio
async def test_a_read_after_the_frame_is_removed_errors_instead_of_reading_the_top(
        frame_server, mcp_client_session, site, profiles):
    plain = profiles[0]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, plain, f"{site}/frames/same")
        json.loads(await _switch(mcp, tab, "#child"))

        # The frame removes itself; the recorded path no longer resolves. The next
        # read reports that rather than quietly answering from the top page...
        await _call_text(mcp, "click", {"css_selector": "#remover", "id": tab})
        refused = await _call_text(mcp, "query_selector", {"css_selector": "#top-only", "id": tab})
        assert "no longer on the page" in refused, refused

        # ...once: the agent now knows the tab is at its top document.
        assert (await _find(mcp, tab, "#top-only"))["found"] is True
        assert (await _find(mcp, tab, "#child"))["found"] is False


# -- frames the parent wrote: judged by the page that wrote them --------------

@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["/frames/srcdoc", "/frames/blank"])
async def test_a_frame_the_page_wrote_is_judged_by_that_page(
        frame_server, mcp_client_session, site, profiles, page):
    plain = profiles[0]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, plain, f"{site}{page}")

        entered = json.loads(await _switch(mcp, tab, "#child"))
        assert entered["frame_url"] == f"{site}{page}"
        # The read gate inside the frame judges it by the same URL.
        found = await _find(mcp, tab, "#in-frame")
        assert found["found"] is True and found["element"]["text"] == "INSIDE"


@pytest.mark.asyncio
async def test_a_sandboxed_srcdoc_frame_is_refused(
        frame_server, mcp_client_session, site, profiles):
    # A sandbox gives the frame an opaque origin, so it can't read its parent's
    # URL and keeps its own about:srcdoc, which no rule admits.
    plain = profiles[0]
    async with mcp_client_session(frame_server) as mcp:
        tab = await _open(mcp, plain, f"{site}/frames/sandboxed")

        assert "frame_url" not in await _switch(mcp, tab, "#child")
        await _assert_at_top(mcp, tab)
