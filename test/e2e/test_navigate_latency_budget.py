"""E2e: per-call latency budgets, measured where the caller feels them.

Not a benchmark — a tripwire. The numbers a shared CI runner produces are too
noisy to assert on tightly, so these budgets sit roughly 5x above the observed
median and only fire on order-of-magnitude regressions. Those are the ones that
have actually happened:

  * issue #182 -- Chrome's password-store detection activates the freedesktop
    secret portal over D-Bus. Where that activation hangs (a failed
    xdg-desktop-portal, the normal state of a non-GNOME or server session),
    Chrome waits out D-Bus's 25s reply timeout and the FIRST navigation of every
    browser launch waits with it. Measured: 25.1s to load a 127.0.0.1 page whose
    TTFB was 4ms.
  * issue #180 -- no page_load_strategy, so drv.get waits for loadEventEnd.
  * issue #181 -- _wait_for_title polls at Selenium's default 500ms.

Everything is served by an in-process ``http.server`` on 127.0.0.1, so a budget
failure is browden or the browser, never the network or a site. The pages carry
a <title>: without one the backend waits out TITLE_WAIT_SECONDS and that, not
the thing under test, would dominate.
"""

import http.server
import json
import os
import sys
import threading
import time

import pytest
import yaml

from browden.web_navigator.utils.network_utils import get_free_port

# A fresh browser session: Chrome launch + attach + first navigation. Chrome
# itself starts in ~0.3s on a developer box but several seconds on a cold,
# loaded runner, so this is deliberately loose -- it still sits ~3x under the
# 25s portal stall of #182, which is what it exists to catch.
COLD_FIRST_NAVIGATE_BUDGET_S = 8.0

# A navigation in an already-running session: no launch, no first-load stalls.
WARM_NAVIGATE_BUDGET_S = 3.0

# A read against an already-parsed page, served from the DOM cache.
CACHED_READ_BUDGET_S = 2.0

_DIAGNOSIS = (
    "\nA first-navigation blowout is usually Chrome's secret-portal activation "
    "timeout (issue #182): check `systemctl --user status xdg-desktop-portal.service` "
    "-- if it is failed, D-Bus activation never returns and Chrome waits 25s. "
    "Masking that unit makes the activation fail fast; BROWDEN_PASSWORD_STORE=basic "
    "also avoids it, but re-keys the profile and permanently drops its cookies."
)


@pytest.fixture
def latency_server(tmp_path):
    """An MCP server that may read localhost, and a local page to read."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    root = tmp_path / "site"
    root.mkdir()
    for name in ("a", "b"):
        (root / f"{name}.html").write_text(
            f"<html><head><title>page {name}</title></head>"
            f"<body><h1 id='hdr'>page {name}</h1><p class='x'>one</p>"
            f"<p class='x'>two</p></body></html>")
    class Quiet(http.server.SimpleHTTPRequestHandler):
        """Serves ``root`` without logging: per-request lines would bury the
        timing report this test prints."""

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    port = get_free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), Quiet)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg = cfg_dir / "allowlist.yaml"
    cfg.write_text(yaml.safe_dump(
        {"read": {"enabled": True, "tranco": {"enabled": False},
                  "website_overrides": {"localhost": [".*"], "127.0.0.1": [".*"]}}},
        sort_keys=False))
    cache = cfg_dir / "cache"
    cache.mkdir()
    harness = McpServerHarness(cache, allowlist_path=cfg)
    harness.start()
    try:
        yield harness, port
    finally:
        harness.stop()
        httpd.shutdown()
        thread.join(timeout=5)


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


def _payload(res) -> dict:
    return json.loads(_text(res))


async def _timed(mcp, tool: str, args: dict) -> tuple[dict, float]:
    """Call a tool over the real SSE transport and time what the caller waits."""
    start = time.perf_counter()
    res = await mcp.call_tool(tool, args)
    elapsed = time.perf_counter() - start
    payload = _payload(res)
    assert "error" not in payload, f"{tool} failed: {payload.get('error')}"
    return payload, elapsed


async def test_navigate_and_read_stay_within_budget(latency_server, tmp_path):
    harness, port = latency_server
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    url_a = f"http://localhost:{port}/a.html"
    url_b = f"http://localhost:{port}/b.html"
    timings: dict[str, float] = {}

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()

            # A profile-dir of its own, so this really is a fresh Chrome launch
            # and the first navigation really is a browser's first navigation --
            # the only moment the #182 stall is observable.
            profile = str(tmp_path / "cold_profile")
            tab, timings["new_blank_tab"] = await _timed(
                mcp, "new_blank_tab", {"profile_dir": profile})
            tab_id = tab["id"]

            page, timings["navigate_cold"] = await _timed(
                mcp, "navigate", {"url": url_a, "id": tab_id})
            assert "page a" in page.get("title", "")

            _, timings["navigate_warm"] = await _timed(
                mcp, "navigate", {"url": url_b, "id": tab_id})

            # First read parses the page; the second is served from the cache.
            _, timings["read_uncached"] = await _timed(
                mcp, "query_selector_all", {"css_selector": "p.x", "id": tab_id})
            _, timings["read_cached"] = await _timed(
                mcp, "query_selector_all", {"css_selector": "p.x", "id": tab_id})

    report = "  ".join(f"{k}={v:.3f}s" for k, v in timings.items())
    print(f"\nlatency: {report}")

    cold = timings["new_blank_tab"] + timings["navigate_cold"]
    assert cold < COLD_FIRST_NAVIGATE_BUDGET_S, (
        f"a fresh session's first navigation took {cold:.3f}s, budget "
        f"{COLD_FIRST_NAVIGATE_BUDGET_S}s ({report}).{_DIAGNOSIS}")
    assert timings["navigate_warm"] < WARM_NAVIGATE_BUDGET_S, (
        f"a warm navigation took {timings['navigate_warm']:.3f}s, budget "
        f"{WARM_NAVIGATE_BUDGET_S}s ({report})")
    assert timings["read_cached"] < CACHED_READ_BUDGET_S, (
        f"a cached read took {timings['read_cached']:.3f}s, budget "
        f"{CACHED_READ_BUDGET_S}s ({report})")
