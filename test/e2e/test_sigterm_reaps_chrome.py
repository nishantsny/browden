"""E2e: a plain SIGTERM to the server must not orphan Chrome (issue #183).

The browser teardown is an ``atexit`` hook, and atexit does not run when a
process dies from a signal's default disposition — so ``kill <pid>`` used to
leave every Chrome the server had launched running, each holding its profile's
``SingletonLock`` and blocking the next server that wanted that profile. Ctrl-C
was always fine, because KeyboardInterrupt unwinds.

Two things normally hide this, and both are stepped around here:

  * ``McpServerHarness.stop`` signals the whole process GROUP, which reaps Chrome
    regardless of what the server does — so this test signals ``harness.pid``
    alone.
  * the deployed unit sets ``KillMode=control-group``, so systemd kills every
    Chrome in the service cgroup.

Parameterised over both signals: SIGINT pins the path that already worked, so a
future change cannot quietly break it while fixing the other.
"""
import json
import os
import signal
import sys
import time

import psutil
import pytest


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


def _chrome_children(pid: int) -> list[psutil.Process]:
    """Every Chrome process under the server, recursively."""
    try:
        server = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    found = []
    for child in server.children(recursive=True):
        try:
            if "chrome" in child.name().lower():
                found.append(child)
        except psutil.Error:
            continue
    return found


def _still_alive(procs: list[psutil.Process]) -> list[psutil.Process]:
    """Those still running — a zombie is gone for our purposes (reaped, awaiting wait())."""
    alive = []
    for p in procs:
        try:
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                alive.append(p)
        except psutil.NoSuchProcess:
            continue
    return alive


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signal_to_the_server_alone_reaps_its_chromes(tmp_path, sig):
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    cache = tmp_path / f"cache_{sig}"
    cache.mkdir()
    harness = McpServerHarness(cache)
    harness.start()
    chromes: list[psutil.Process] = []
    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(harness.url) as streams:
            async with ClientSession(*streams) as mcp:
                await mcp.initialize()
                # A real browser, so there is something to orphan.
                tab = json.loads(_text(await mcp.call_tool("new_blank_tab", {})))
                assert "id" in tab, tab

        chromes = _chrome_children(harness.pid)
        assert chromes, "no Chrome under the server — nothing for this test to prove"

        # The server process only. NOT the process group: that would reap Chrome
        # by itself and the test would pass no matter what the server does.
        os.kill(harness.pid, sig)

        deadline = time.time() + 20.0
        while time.time() < deadline:
            if harness._proc.poll() is not None and not _still_alive(chromes):
                break
            time.sleep(0.2)

        assert harness._proc.poll() is not None, f"server survived {sig.name}"
        survivors = _still_alive(chromes)
        assert not survivors, (
            f"{sig.name} orphaned {len(survivors)} Chrome process(es) "
            f"{[p.pid for p in survivors]}: the shutdown hook did not run, so each "
            f"still holds its profile's SingletonLock (issue #183)")
    finally:
        for p in _still_alive(chromes):
            try:
                p.kill()
            except psutil.Error:
                pass
        harness.stop()
