"""E2e: profile-scoped allowlist rules, end-to-end through the MCP server (#139).

A real server process under a config whose rules are scoped to *profile
directories*, driven through the MCP tools against real (headless) Chrome and a
tiny local HTTP server. Two Chrome profiles run side by side under one policy,
and the point of the feature is that the same action on the same URL comes out
differently in each:

* **write** — the click rule lives under the shopper profile only, so the same
  button on the same page is clicked there and refused in the reader profile.
* **read** — the reader profile's ``website_overrides`` admits a page no other
  profile may load, and the shopper profile is refused it.
* **hot reload** — a per-profile rule added to the live config takes effect
  without a restart, in the profile it names and no other.

The pages come from ``127.0.0.1`` (opted in for plain http by an explicit
override), so every load is local and offline.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browden.configs.loader.refresher import DEFAULT_RELOAD_INTERVAL_SECONDS
from browden.web_navigator.utils.network_utils import get_free_port

_PAGE = b"<html><body><button id='atc'>Add to cart</button></body></html>"

# Generous margin over one poll interval: the poller sleeps a full interval
# before its first re-stat, so a reload can't be observed sooner than that.
_RELOAD_DEADLINE = DEFAULT_RELOAD_INTERVAL_SECONDS + 15


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


def _config(shopper, reader, *, reader_click: bool = False) -> str:
    """The policy under test: one global read grant, two profiles that differ."""
    reader_rules = (
        "    click:\n"
        "      127.0.0.1:\n"
        "        - path: ['^/dp/.*']\n"
        "          label: '(?i)add to cart'\n"
    ) if reader_click else ""
    return (
        "read:\n"
        "  enabled: true\n"
        "  tranco: {enabled: false}\n"
        "  website_overrides:\n"
        "    127.0.0.1:\n"
        "      - path: ['^/dp/.*']\n"      # every profile may read the product page
        "profiles:\n"
        f"  {shopper}:\n"
        "    click:\n"
        "      127.0.0.1:\n"
        "        - path: ['^/dp/.*']\n"
        "          label: '(?i)add to cart'\n"
        f"  {reader}:\n"
        "    read:\n"
        "      website_overrides:\n"
        "        127.0.0.1:\n"
        "          - path: ['^/reader-only/.*']\n"   # readable in this profile only
        + reader_rules
    )


@pytest.fixture
def profiles(tmp_path):
    """The two profile directories the config scopes rules to (canonical paths)."""
    shopper = (tmp_path / "shopper-profile").resolve()
    reader = (tmp_path / "reader-profile").resolve()
    return shopper, reader


@pytest.fixture
def profile_scoped_server(tmp_path, profiles):
    """A fresh MCP server whose click/read rules are scoped per profile dir."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    shopper, reader = profiles
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(_config(shopper, reader))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    harness = McpServerHarness(cache_dir, allowlist_path=allowlist)
    harness.allowlist_file = allowlist  # the tests rewrite it to trigger a reload
    harness.start()
    yield harness
    harness.stop()


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


async def _new_tab_id(mcp, profile_dir) -> str:
    res = await mcp.call_tool("new_blank_tab", {"profile_dir": str(profile_dir)})
    return json.loads(res.content[0].text)["id"]


async def _call_text(mcp, tool: str, args: dict) -> str:
    """Call a tool and return its result text — a refusal message on a refused
    action, the JSON result on success. The SDK surfaces a server-side
    ValidationError as either an error result or a raised exception, so fold both
    into the returned text."""
    try:
        return _text(await mcp.call_tool(tool, args))
    except Exception as e:  # noqa: BLE001 — the SDK may raise on a ValidationError
        return str(e)


# -- write: a click rule authorizes one profile ------------------------------

@pytest.mark.asyncio
async def test_click_is_authorized_in_one_profile_and_refused_in_the_other(
        profile_scoped_server, mcp_client_session, site, profiles):
    shopper, reader = profiles
    async with mcp_client_session(profile_scoped_server) as mcp:
        shop_tab = await _new_tab_id(mcp, shopper)
        read_tab = await _new_tab_id(mcp, reader)

        # Both profiles may READ the page — the global override covers /dp/*.
        for tab in (shop_tab, read_tab):
            assert "allowlist" not in (
                await _call_text(mcp, "navigate", {"url": f"{site}/dp/x", "id": tab})).lower()

        # Only the shopper profile may click the button on it.
        assert "clicked" in (await _call_text(mcp, "click", {"css_selector": "#atc", "id": shop_tab}))
        assert "not allowed on this page" in (
            await _call_text(mcp, "click", {"css_selector": "#atc", "id": read_tab}))


# -- read: an override scoped to one profile ---------------------------------

@pytest.mark.asyncio
async def test_read_override_admits_only_the_profile_that_names_it(
        profile_scoped_server, mcp_client_session, site, profiles):
    shopper, reader = profiles
    async with mcp_client_session(profile_scoped_server) as mcp:
        shop_tab = await _new_tab_id(mcp, shopper)
        read_tab = await _new_tab_id(mcp, reader)

        url = f"{site}/reader-only/7"
        assert "allowlist" not in (
            await _call_text(mcp, "navigate", {"url": url, "id": read_tab})).lower()
        assert "allowlist" in (
            await _call_text(mcp, "navigate", {"url": url, "id": shop_tab})).lower()


# -- hot reload: a per-profile edit lands without a restart -------------------

@pytest.mark.asyncio
async def test_a_profile_rule_added_live_takes_effect_without_a_restart(
        profile_scoped_server, mcp_client_session, site, profiles):
    shopper, reader = profiles
    async with mcp_client_session(profile_scoped_server) as mcp:
        read_tab = await _new_tab_id(mcp, reader)
        await mcp.call_tool("navigate", {"url": f"{site}/dp/x", "id": read_tab})
        assert "not allowed on this page" in (
            await _call_text(mcp, "click", {"css_selector": "#atc", "id": read_tab}))

        # Grant the reader profile the same click rule, in the file the running
        # server watches — no restart, and the tab stays open across the reload.
        profile_scoped_server.allowlist_file.write_text(
            _config(shopper, reader, reader_click=True))

        deadline = time.time() + _RELOAD_DEADLINE
        while time.time() < deadline:
            if "clicked" in (await _call_text(
                    mcp, "click", {"css_selector": "#atc", "id": read_tab})):
                return
            time.sleep(1.0)
        pytest.fail("the per-profile click rule never took effect after a hot reload")
