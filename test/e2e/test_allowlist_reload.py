"""E2e: editing the allowlist hot-reloads into the running server and flips the gate.

A real server process (with ``BROWDEN_RELOAD_INTERVAL`` tightened so the poller
ticks fast) whose config allows ``website-reload-xyz.com`` via a local Tranco
snapshot. The first ``navigate`` passes the read gate; the config is then edited
so the host is no longer listed, and within a few poller ticks the same
``navigate`` must be refused — proving the reloaded policy answers fresh and no
memoized allow (``PopularityAllowlist.contains``) survives the swap.

Offline & deterministic, like test_psl_read_gate: the refusal happens in
``validate_url`` before any page load, and the allowed navigation targets a
made-up listed domain, so DNS fails fast and the test never touches a live site.
"""
import asyncio
import gzip
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

# A made-up registrable domain standing in for the reviewer's "website.com" —
# fictitious so the allowed navigation never resolves (the suite stays offline).
_HOST = "website-reload-xyz.com"
_URL = f"https://{_HOST}/"

# top_n: 10 covers both lines; the tightened edit (top_n: 1) keeps only the
# first, so _HOST falls off the allowlist without touching the snapshot file
# (the refresher watches the config file, not the snapshot).
_SNAPSHOT = ["anchor-xyz.com", _HOST]
_ALLOWED_CFG = "read:\n  enabled: true\n  tranco: {enabled: true, top_n: 10}\n"
_DISALLOWED_CFG = "read:\n  enabled: true\n  tranco: {enabled: true, top_n: 1}\n# tightened\n"


def _reload_config_dir(tmp_path: Path) -> Path:
    """allowlist.yaml + Tranco snapshot + PSL, the layout the read gate loads."""
    import publicsuffix2

    d = tmp_path / "reload_cfg"
    d.mkdir()
    (d / "allowlist.yaml").write_text(_ALLOWED_CFG)
    with gzip.open(d / "tranco-top-400k.txt.gz", "wt", encoding="utf-8") as fh:
        fh.write("\n".join(_SNAPSHOT) + "\n")
    shutil.copy(Path(publicsuffix2.__file__).resolve().parent / "public_suffix_list.dat",
                d / "public_suffix_list.dat")
    return d


@pytest.fixture
def reload_mcp_server(tmp_path, monkeypatch):
    """A fresh MCP server watching the config above, polling every 0.2s."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    cfg = _reload_config_dir(tmp_path)
    monkeypatch.setenv("BROWDEN_RELOAD_INTERVAL", "0.2")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    harness = McpServerHarness(cache_dir, allowlist_path=cfg / "allowlist.yaml")
    harness.start()
    yield harness, cfg / "allowlist.yaml"
    harness.stop()


async def _new_tab_id(mcp) -> str:
    res = await mcp.call_tool("new_blank_tab", {})
    return json.loads(res.content[0].text)["id"]


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


async def _navigate_refused(mcp, url, handle) -> bool:
    """True iff the READ GATE refused ``url`` (see test_psl_read_gate)."""
    try:
        res = await mcp.call_tool("navigate", {"url": url, "id": handle})
        return "allowlist" in _text(res).lower()
    except Exception as e:  # SDK may raise on a server-side ValidationError
        return "allowlist" in str(e).lower()


@pytest.mark.asyncio
async def test_hot_reload_to_disallowed_refuses_navigation(reload_mcp_server, mcp_client_session):
    harness, allowlist_path = reload_mcp_server
    async with mcp_client_session(harness) as mcp:
        handle = await _new_tab_id(mcp)
        # Listed -> the gate must NOT refuse (the DNS-level load failure that
        # follows is a navigation outcome, not a read-gate refusal).
        assert not await _navigate_refused(mcp, _URL, handle)

        allowlist_path.write_text(_DISALLOWED_CFG)  # host falls off the list

        # Within a few 0.2s poller ticks the running server must swap the fresh
        # policy in and refuse the very same navigation.
        deadline = time.time() + 15.0
        refused = False
        while time.time() < deadline and not refused:
            refused = await _navigate_refused(mcp, _URL, handle)
            if not refused:
                await asyncio.sleep(0.3)
        assert refused, "allowlist edit was never picked up by the hot-reload poller"
