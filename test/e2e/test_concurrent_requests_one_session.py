"""E2e: ten tabs inside ONE browser session, read concurrently.

Guards the per-session driver lock (``BrowserSessionManager._driver_lock``).
One profile-dir is one browser session, one WebDriver, and Selenium's driver is
not thread-safe — while the MCP server dispatches every incoming request with
``tg.start_soon``, so ten in-flight ``query_selector`` calls really do arrive at
one session at once.

What breaks without the lock is *content cross-talk*, not an exception. A read
is two driver round-trips — ``select_tab(handle)`` then ``get_tab_html()`` (see
``SoupCache.get_soup``) — so unsynchronized readers interleave and a tab comes
back with whichever document won the last ``select_tab``. Before the lock this
test failed with 6/10 tabs returning another tab's page; with it, each request
waits its turn and re-focuses its own tab, so all ten read their own content.

The sequential control pass first proves all ten tabs are individually readable,
so a failure in the concurrent pass can only be the concurrency.

Offline and deterministic like the rest of the suite: the ten pages are served
by an in-process ``http.server`` on 127.0.0.1 and read through a ``localhost``
allowlist override (the same opt-in ``test_read_scheme_gate.py`` uses).
"""
import asyncio
import functools
import http.server
import json
import os
import sys
import threading

import pytest
import yaml

from browden.web_navigator.utils.network_utils import get_free_port

TAB_COUNT = 10


def _token(i: int) -> str:
    return f"TAB_TOKEN_{i}"


@pytest.fixture
def make_server(tmp_path):
    """make(config: dict) -> started McpServerHarness under that allowlist."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    started: list = []

    def make(config: dict):
        d = tmp_path / "cfg"
        d.mkdir()
        cfg = d / "allowlist.yaml"
        cfg.write_text(yaml.safe_dump(config, sort_keys=False))
        cache = d / "cache"
        cache.mkdir()
        harness = McpServerHarness(cache, allowlist_path=cfg)
        harness.start()
        started.append(harness)
        return harness

    yield make

    for harness in started:
        harness.stop()


@pytest.fixture
def local_http_site(tmp_path):
    """Serve ``/p<i>.html`` for i in range(TAB_COUNT), each with its own token."""
    root = tmp_path / "site"
    root.mkdir()
    for i in range(TAB_COUNT):
        # A <title> matters: the backend waits out TITLE_WAIT_SECONDS after every
        # navigation to a page that never sets one, which would dominate the runtime.
        (root / f"p{i}.html").write_text(
            f"<html><head><title>{_token(i)}</title></head>"
            f"<body><h1 id='hdr'>{_token(i)}</h1></body></html>")

    port = get_free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    # Quiet: ten tabs * two passes of request logging is noise in the e2e output.
    httpd.RequestHandlerClass.log_message = lambda *a, **k: None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


def _header_text(res) -> str:
    """The <h1 id=hdr> text a query_selector result carries, or a marker string."""
    payload = json.loads(_text(res))
    if "error" in payload:
        return f"<error: {payload['error']}>"
    if not payload.get("found"):
        return "<not found>"
    return payload["element"]["text"]


async def test_ten_tabs_in_one_session_read_concurrently(make_server, local_http_site):
    port = local_http_site
    harness = make_server({"read": {"enabled": True, "tranco": {"enabled": False},
                                    "website_overrides": {"localhost": [".*"]}}})
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()

            # 1. Ten tabs, no profile_dir -> all in the DEFAULT profile, i.e. one
            #    browser session sharing one WebDriver. Opened sequentially: tab
            #    creation is not what's under test.
            ids = []
            for i in range(TAB_COUNT):
                res = await mcp.call_tool("new_blank_tab", {})
                ids.append(json.loads(_text(res))["id"])
            assert len({i.partition("-")[0] for i in ids}) == 1, \
                "all ten tabs must land in one session for this test to mean anything"

            async def navigate_all() -> None:
                for i, tab_id in enumerate(ids):
                    await mcp.call_tool(
                        "navigate", {"url": f"http://localhost:{port}/p{i}.html", "id": tab_id})

            # 2. Control pass: sequential reads. Every tab is individually
            #    readable and holds its own token, so anything the concurrent
            #    pass gets wrong is the concurrency, not the fixture.
            await navigate_all()
            sequential = {}
            for i, tab_id in enumerate(ids):
                res = await mcp.call_tool("query_selector", {"css_selector": "#hdr", "id": tab_id})
                sequential[i] = _header_text(res)
            assert sequential == {i: _token(i) for i in range(TAB_COUNT)}, \
                f"sequential reads already disagree, fixture is broken: {sequential}"

            # 3. Re-navigate to drop every tab's cached soup (navigate invalidates),
            #    so the concurrent pass below actually hits the driver — a warm
            #    cache would answer from memory and hide the race.
            await navigate_all()

            # 4. The same ten reads, all in flight at once.
            results = await asyncio.gather(*(
                mcp.call_tool("query_selector", {"css_selector": "#hdr", "id": tab_id})
                for tab_id in ids), return_exceptions=True)

            concurrent = {}
            for i, res in enumerate(results):
                concurrent[i] = f"<raised: {res!r}>" if isinstance(res, BaseException) \
                    else _header_text(res)

            wrong = {i: got for i, got in concurrent.items() if got != _token(i)}
            assert not wrong, (
                f"{len(wrong)}/{TAB_COUNT} concurrent reads on one session returned the "
                f"wrong tab's document (expected TAB_TOKEN_<i> for tab <i>): {wrong}")
