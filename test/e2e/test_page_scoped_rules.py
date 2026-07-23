"""E2e: page-scoped read overrides and write rules, end-to-end through the MCP server.

A real server process under a custom allowlist whose ``127.0.0.1`` rules are
*per page*, driven through the ``navigate`` / ``click`` MCP tools against a real
(headless) Chrome and a tiny local HTTP server. Proves the two halves of the
feature against live Chrome, not just the unit gates:

* **read** — a page-scoped ``website_overrides`` entry admits only the pages its
  rules match (``/products/<n>`` and, via ``match_on: url``, the hash-router page
  ``/#/reports/<n>``) and refuses every other page on the same host — even though
  listing the host at all is what demotes it from a blanket grant.
* **write** — a ``click`` host whose label is scoped per page: "Add to cart" is
  clickable only on ``/dp/*`` and "Place your order" only on ``/gp/buy/*``, so the
  *same* button is allowed on one page and refused on another, and a page with no
  click rule at all refuses before touching the DOM.

The pages are served from ``127.0.0.1`` (opted in for plain http by the explicit
override), so every load is local and offline.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browden.web_navigator.utils.network_utils import get_free_port

# Every path serves the same page: two labelled buttons the click tests act on.
# The server never sees the URL fragment, which is exactly why match_on: url is
# needed to distinguish hash-router pages — the distinction is client-side only.
_PAGE = (
    "<html><body>"
    "<button id='atc'>Add to cart</button>"
    "<button id='order'>Place your order</button>"
    "</body></html>"
).encode()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *args):  # keep the test output quiet
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


@pytest.fixture
def page_scoped_server(tmp_path):
    """A fresh MCP server whose 127.0.0.1 read + click rules are page-scoped."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(
        "read:\n"
        "  enabled: true\n"
        "  tranco: {enabled: false}\n"
        "  website_overrides:\n"
        "    127.0.0.1:\n"
        "      - path: ['^/products/\\d+$']\n"          # match_on: path (default)
        "      - path: ['^/dp/.*', '^/gp/buy/.*']\n"    # the write-flow pages
        "      - path: ['^/#/reports/\\d+$']\n"         # a hash-router SPA page
        "        match_on: url\n"
        "click:\n"
        "  127.0.0.1:\n"
        "    - path: ['^/dp/.*']\n"
        "      label: '(?i)add to cart'\n"
        "    - path: ['^/gp/buy/.*']\n"
        "      label: '(?i)place your order'\n"
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    harness = McpServerHarness(cache_dir, allowlist_path=allowlist)
    harness.start()
    yield harness
    harness.stop()


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


async def _new_tab_id(mcp) -> str:
    res = await mcp.call_tool("new_blank_tab", {})
    return json.loads(res.content[0].text)["id"]


async def _call_text(mcp, tool: str, args: dict) -> str:
    """Call a tool and return its result text — an ``allowlist``/refusal message on
    a refused action, the JSON result on success. The SDK surfaces a server-side
    ValidationError as either an error result or a raised exception, so fold both
    into the returned text."""
    try:
        return _text(await mcp.call_tool(tool, args))
    except Exception as e:  # noqa: BLE001 — the SDK may raise on a ValidationError
        return str(e)


# -- read: page-scoped website_overrides -------------------------------------

@pytest.mark.asyncio
async def test_read_override_is_page_scoped(page_scoped_server, mcp_client_session, site):
    async with mcp_client_session(page_scoped_server) as mcp:
        handle = await _new_tab_id(mcp)

        # An in-scope path loads (no allowlist refusal); an off-scope path and a
        # near-miss (\d+) are both refused at the input gate, before any load.
        assert "allowlist" not in (await _call_text(mcp, "navigate", {"url": f"{site}/products/7", "id": handle})).lower()
        assert "allowlist" in (await _call_text(mcp, "navigate", {"url": f"{site}/products/abc", "id": handle})).lower()
        assert "allowlist" in (await _call_text(mcp, "navigate", {"url": f"{site}/secret", "id": handle})).lower()


@pytest.mark.asyncio
async def test_read_override_match_on_url_scopes_hash_router(page_scoped_server, mcp_client_session, site):
    async with mcp_client_session(page_scoped_server) as mcp:
        handle = await _new_tab_id(mcp)

        # Both share the path "/" — only match_on: url tells them apart. The
        # reports page is allowed (and re-gates clean after loading with its
        # fragment intact); the admin page is refused.
        assert "allowlist" not in (await _call_text(mcp, "navigate", {"url": f"{site}/#/reports/3", "id": handle})).lower()
        assert "allowlist" in (await _call_text(mcp, "navigate", {"url": f"{site}/#/admin", "id": handle})).lower()


# -- write: page-scoped click labels -----------------------------------------

@pytest.mark.asyncio
async def test_click_label_is_scoped_to_its_page(page_scoped_server, mcp_client_session, site):
    async with mcp_client_session(page_scoped_server) as mcp:
        handle = await _new_tab_id(mcp)

        # On /dp/*, "Add to cart" is authorized; "Place your order" is not (its
        # rule only matches /gp/buy/*).
        await mcp.call_tool("navigate", {"url": f"{site}/dp/x", "id": handle})
        assert "clicked" in (await _call_text(mcp, "click", {"css_selector": "#atc", "id": handle}))
        assert "does not match any click label" in (
            await _call_text(mcp, "click", {"css_selector": "#order", "id": handle}))

        # The exact mirror on /gp/buy/*: the two labels swap which is allowed.
        await mcp.call_tool("navigate", {"url": f"{site}/gp/buy/x", "id": handle})
        assert "clicked" in (await _call_text(mcp, "click", {"css_selector": "#order", "id": handle}))
        assert "does not match any click label" in (
            await _call_text(mcp, "click", {"css_selector": "#atc", "id": handle}))


@pytest.mark.asyncio
async def test_click_refused_on_readable_page_with_no_click_rule(page_scoped_server, mcp_client_session, site):
    async with mcp_client_session(page_scoped_server) as mcp:
        handle = await _new_tab_id(mcp)

        # /products/<n> is readable (navigable) but has no click rule at all, so a
        # click is refused at gate 1 — before the element is even queried.
        await mcp.call_tool("navigate", {"url": f"{site}/products/7", "id": handle})
        refusal = await _call_text(mcp, "click", {"css_selector": "#atc", "id": handle})
        assert "not allowed on this page" in refusal
