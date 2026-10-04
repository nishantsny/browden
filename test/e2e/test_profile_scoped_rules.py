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
* **allow_all** — a scratch profile that opts into everything clicks a control
  no rule mentions, while a second allow_all profile that left the popularity
  net on still refuses a host the snapshot doesn't rank.
* **hot reload** — a per-profile rule added to the live config takes effect
  without a restart, in the profile it names and no other.
* **denylist** — a profile's own denylist entry refuses a page the global rules
  allow, in that profile only.
* **list_tabs** — each profile's tabs are judged by that profile's rules, so a
  tab only its own profile may read is still listed.
* **redirect landing** — ``navigate`` re-gates where a redirect lands against
  the *target tab's* profile, so the same 302 is kept in one profile and
  bounced to ``about:blank`` in the other.

The e2e config dir has no Tranco snapshot next to it, so the popularity check
matches nothing here — which is exactly what makes "allow_all keeps the net on"
observable offline: with the net on, an unranked host is refused; with
``tranco: {enabled: false}`` in the same profile, it is admitted.

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

from safe_agent_browser.configs.loader.refresher import DEFAULT_RELOAD_INTERVAL_SECONDS
from safe_agent_browser.web_navigator.utils.network_utils import get_free_port

_PAGE = b"<html><body><button id='atc'>Add to cart</button></body></html>"
# A /dp/* page (readable by every profile) that 302s to a page only the reader
# profile may read — the landing, not the input URL, is what differs by profile.
_REDIRECT_PATH = "/dp/go"
_REDIRECT_TARGET = "/reader-only/7"

# Generous margin over one poll interval: the poller sleeps a full interval
# before its first re-stat, so a reload can't be observed sooner than that.
_RELOAD_DEADLINE = DEFAULT_RELOAD_INTERVAL_SECONDS + 15


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == _REDIRECT_PATH:
            self.send_response(302)
            self.send_header("Location", _REDIRECT_TARGET)
            self.end_headers()
            return
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


def _config(shopper, reader, scratch, guarded, *, reader_click: bool = False) -> str:
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
        "    denylist:\n"
        "      127.0.0.1: ['^/dp/blocked']\n"   # refused here though /dp/* is globally readable
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
        # Opts into everything, popularity net explicitly off: anything goes here.
        + f"  {scratch}:\n"
          "    allow_all: true\n"
          "    read:\n"
          "      tranco: {enabled: false}\n"
        # Opts into everything but leaves the net ON — the default under
        # allow_all — so an unranked host is still refused.
        + f"  {guarded}:\n"
          "    allow_all: true\n"
    )


@pytest.fixture
def profiles(tmp_path):
    """The profile directories the config scopes rules to (canonical paths)."""
    return tuple((tmp_path / f"{name}-profile").resolve()
                 for name in ("shopper", "reader", "scratch", "guarded"))


@pytest.fixture
def profile_scoped_server(tmp_path, profiles):
    """A fresh MCP server whose click/read rules are scoped per profile dir."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(_config(*profiles))
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
    shopper, reader = profiles[0], profiles[1]
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
    shopper, reader = profiles[0], profiles[1]
    async with mcp_client_session(profile_scoped_server) as mcp:
        shop_tab = await _new_tab_id(mcp, shopper)
        read_tab = await _new_tab_id(mcp, reader)

        url = f"{site}/reader-only/7"
        assert "allowlist" not in (
            await _call_text(mcp, "navigate", {"url": url, "id": read_tab})).lower()
        assert "allowlist" in (
            await _call_text(mcp, "navigate", {"url": url, "id": shop_tab})).lower()


# -- allow_all: everything, in one profile, with the net still on ------------

@pytest.mark.asyncio
async def test_allow_all_profile_clicks_a_control_no_rule_mentions(
        profile_scoped_server, mcp_client_session, site, profiles):
    scratch = profiles[2]
    async with mcp_client_session(profile_scoped_server) as mcp:
        tab = await _new_tab_id(mcp, scratch)

        # A page no read rule covers, and a button no click rule names: both are
        # allowed here, and nowhere else in this config.
        assert "allowlist" not in (
            await _call_text(mcp, "navigate", {"url": f"{site}/anything/at/all", "id": tab})).lower()
        assert "clicked" in (await _call_text(mcp, "click", {"css_selector": "#atc", "id": tab}))


@pytest.mark.asyncio
async def test_allow_all_keeps_the_popularity_net_on_unless_told_otherwise(
        profile_scoped_server, mcp_client_session, site, profiles):
    scratch, guarded = profiles[2], profiles[3]
    async with mcp_client_session(profile_scoped_server) as mcp:
        scratch_tab = await _new_tab_id(mcp, scratch)
        guarded_tab = await _new_tab_id(mcp, guarded)

        # An unranked host (nothing ranks in this run — no snapshot next to the
        # config): refused in the allow_all profile that kept the net, admitted
        # in the one that opted out. The admitted navigation then fails DNS,
        # which is a navigation outcome, not a gate refusal.
        url = "https://unranked-xyz-9876.test/x"
        assert "allowlist" in (
            await _call_text(mcp, "navigate", {"url": url, "id": guarded_tab})).lower()
        assert "allowlist" not in (
            await _call_text(mcp, "navigate", {"url": url, "id": scratch_tab})).lower()


# -- hot reload: a per-profile edit lands without a restart -------------------

@pytest.mark.asyncio
async def test_a_profile_rule_added_live_takes_effect_without_a_restart(
        profile_scoped_server, mcp_client_session, site, profiles):
    reader = profiles[1]
    async with mcp_client_session(profile_scoped_server) as mcp:
        read_tab = await _new_tab_id(mcp, reader)
        await mcp.call_tool("navigate", {"url": f"{site}/dp/x", "id": read_tab})
        assert "not allowed on this page" in (
            await _call_text(mcp, "click", {"css_selector": "#atc", "id": read_tab}))

        # Grant the reader profile the same click rule, in the file the running
        # server watches — no restart, and the tab stays open across the reload.
        profile_scoped_server.allowlist_file.write_text(
            _config(*profiles, reader_click=True))

        deadline = time.time() + _RELOAD_DEADLINE
        while time.time() < deadline:
            if "clicked" in (await _call_text(
                    mcp, "click", {"css_selector": "#atc", "id": read_tab})):
                return
            time.sleep(1.0)
        pytest.fail("the per-profile click rule never took effect after a hot reload")


# -- denylist: a profile's own entry beats a global allow, in that profile ------

@pytest.mark.asyncio
async def test_a_profile_denylist_entry_refuses_only_in_that_profile(
        profile_scoped_server, mcp_client_session, site, profiles):
    shopper, reader = profiles[0], profiles[1]
    async with mcp_client_session(profile_scoped_server) as mcp:
        shop_tab = await _new_tab_id(mcp, shopper)
        read_tab = await _new_tab_id(mcp, reader)

        # /dp/* is readable by every profile, but the shopper profile denylists
        # /dp/blocked: the denial must hold there (scoping it the wrong way would
        # fail OPEN) and must not leak into the reader profile.
        url = f"{site}/dp/blocked"
        assert "allowlist" in (
            await _call_text(mcp, "navigate", {"url": url, "id": shop_tab})).lower()
        assert "allowlist" not in (
            await _call_text(mcp, "navigate", {"url": url, "id": read_tab})).lower()


# -- list_tabs: each profile's tabs are judged by its own rules --------------

@pytest.mark.asyncio
async def test_list_tabs_keeps_a_tab_only_its_own_profile_may_read(
        profile_scoped_server, mcp_client_session, site, profiles):
    shopper, reader = profiles[0], profiles[1]
    async with mcp_client_session(profile_scoped_server) as mcp:
        shop_tab = await _new_tab_id(mcp, shopper)
        read_tab = await _new_tab_id(mcp, reader)
        await mcp.call_tool("navigate", {"url": f"{site}/dp/x", "id": shop_tab})
        await mcp.call_tool("navigate", {"url": f"{site}{_REDIRECT_TARGET}", "id": read_tab})

        # list_tabs closes and hides any tab its gate refuses. The reader tab's
        # page is readable under the reader profile's rules only, so it survives
        # the listing only if it is judged by its own profile, not the global set
        # or the shopper's.
        res = await mcp.call_tool("list_tabs", {})
        listed = {}
        for c in res.content:
            if c.type == "text":
                data = json.loads(c.text)
                for tab in (data if isinstance(data, list) else [data]):
                    listed[tab.get("id")] = tab.get("url") or ""
        assert listed.get(read_tab, "").endswith(_REDIRECT_TARGET), listed
        assert listed.get(shop_tab, "").endswith("/dp/x"), listed


# -- navigate: the redirect landing is judged by the target tab's profile ----

@pytest.mark.asyncio
async def test_a_redirect_landing_is_judged_by_the_target_tabs_profile(
        profile_scoped_server, mcp_client_session, site, profiles):
    shopper, reader = profiles[0], profiles[1]
    async with mcp_client_session(profile_scoped_server) as mcp:
        shop_tab = await _new_tab_id(mcp, shopper)
        read_tab = await _new_tab_id(mcp, reader)

        # The input URL (/dp/go) is readable by both profiles; it 302s to a page
        # only the reader profile may read. The landing is re-gated against the
        # tab's own profile: kept in the reader tab, bounced in the shopper tab.
        url = f"{site}{_REDIRECT_PATH}"
        landed = await _call_text(mcp, "navigate", {"url": url, "id": read_tab})
        assert "left the allowlist" not in landed and _REDIRECT_TARGET in landed, landed
        bounced = await _call_text(mcp, "navigate", {"url": url, "id": shop_tab})
        assert "navigation left the allowlist" in bounced, bounced
