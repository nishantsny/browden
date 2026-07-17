"""E2e for live allowlist reload (#88) through a real MCP server.

The server re-stats its allowlist file every ~10s and atomically swaps in a
freshly-loaded policy on change, so an operator can tighten or loosen the gate
without a restart. These drive a live server process and edit the very file it
watches, then poll a gated ``navigate`` until (or to confirm it never does) the
new policy is observable — exercising loader -> AllowlistRefresher.maybe_reload
-> the swapped ReadPolicy -> validate_url -> the navigate tool.

Two properties, end to end:
  * a real edit takes effect within a poll interval, no restart; and
  * a *broken* edit is ignored — the last-good policy stays in force (a security
    gate must never fall open on a mid-edit save).

Offline & deterministic: every gated URL is a made-up host, so refusals happen
in ``validate_url`` before any load and an *allowed* host merely fails DNS (a
navigation outcome, never a gate refusal — which is all these assert).
"""
import asyncio
import json
import os
import sys
import time

import pytest
import yaml

from browden.configs.loader.refresher import DEFAULT_RELOAD_INTERVAL_SECONDS

# Generous margin over one poll interval: the poller sleeps a full interval
# before its first re-stat, so a reload can't be observed sooner than that.
_RELOAD_DEADLINE = DEFAULT_RELOAD_INTERVAL_SECONDS + 15


@pytest.fixture
def make_server(tmp_path):
    """make(config: dict) -> started McpServerHarness watching that allowlist file.

    Rewrite ``harness.allowlist_path`` in the test to trigger a hot-reload. Each
    call gets its own config dir; every harness is stopped on teardown.
    """
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    started: list = []
    counter = {"n": 0}

    def make(config: dict):
        d = tmp_path / f"cfg{counter['n']}"
        counter["n"] += 1
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


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


async def _new_tab_id(mcp) -> str:
    res = await mcp.call_tool("new_blank_tab", {})
    return json.loads(res.content[0].text)["id"]


async def _is_allowlist_refused(mcp, url: str, handle: str) -> bool:
    """True iff the read gate refused ``url`` with its ``not on allowlist`` phrase.

    A host the policy *allows* still fails to load (made-up domain, DNS miss),
    but that carries a network message, never the allowlist signature — so this
    keys on the gate phrase alone, distinguishing "refused" from "allowed but
    unreachable".
    """
    try:
        res = await mcp.call_tool("navigate", {"url": url, "id": handle})
        msg = _text(res).lower()
    except Exception as e:
        msg = str(e).lower()
    return "not on allowlist" in msg


async def _wait_until(predicate, deadline_s: float) -> bool:
    """Poll ``predicate()`` (async, -> bool) every 0.5s until True or timeout."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if await predicate():
            return True
        await asyncio.sleep(0.5)
    return False


_HOST = "reload-host-xyz-987.test"
_DENY_ALL = {"read": {"enabled": True, "tranco": {"enabled": False}}}
_ALLOW_HOST = {"read": {"enabled": True, "tranco": {"enabled": False},
                        "website_overrides": {_HOST: [".*"]}}}


@pytest.mark.asyncio
async def test_edit_to_allowlist_takes_effect_without_restart(make_server):
    harness = make_server(_DENY_ALL)
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)
            url = f"https://{_HOST}/"

            # Under the initial deny-all policy the host is refused.
            assert await _is_allowlist_refused(mcp, url, handle)

            # Loosen the *watched* file to allow the host — no restart.
            harness.allowlist_path.write_text(yaml.safe_dump(_ALLOW_HOST, sort_keys=False))

            # Within a poll interval the swapped policy stops refusing it.
            async def _now_allowed() -> bool:
                return not await _is_allowlist_refused(mcp, url, handle)

            flipped = await _wait_until(_now_allowed, _RELOAD_DEADLINE)
            assert flipped, "hot-reload did not pick up the edited allowlist in time"


@pytest.mark.asyncio
async def test_broken_edit_keeps_last_good_policy(make_server):
    harness = make_server(_ALLOW_HOST)
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)
            url = f"https://{_HOST}/"

            # The starting policy allows the host (not an allowlist refusal).
            assert not await _is_allowlist_refused(mcp, url, handle)

            # Corrupt the watched file: unparseable YAML. maybe_reload must keep
            # the last-good policy rather than fall open or crash.
            harness.allowlist_path.write_text('read: {enabled: true\n  broken: [')

            # Wait past a full poll interval so the poller has re-stat'd and tried
            # (and rejected) the broken file at least once.
            await asyncio.sleep(_RELOAD_DEADLINE)

            # Still allowed: the good policy from before the bad save is in force.
            assert not await _is_allowlist_refused(mcp, url, handle)
