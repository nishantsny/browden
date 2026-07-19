#!/usr/bin/env python3
"""Profile the resource usage of the browden MCP *Python server process only*.

Chrome and chromedriver spawn as separate child PIDs and are deliberately NOT
measured — this samples exactly the `python -m browden.mcp.server` process, so
the numbers are the server's own footprint (selenium client objects, session
bookkeeping, cached DOM/screenshot payloads, chromedriver sockets, asyncio).

What it does
------------
1. Spawns the real server over SSE (reusing the e2e McpServerHarness) under an
   allow-all, Tranco-disabled allowlist, and serves a small local HTML page so
   navigation is hermetic (no external network).
2. Samples the server PID's USS/RSS/CPU/threads/FDs at a fixed interval in a
   background thread, tagging each sample with the current workload phase.
3. Drives a phased workload through a real MCP ClientSession, then prints a
   per-phase table (median USS/RSS/FD/threads, delta-vs-idle, peak CPU) and
   writes the raw samples to CSV.

Run:
    uv run --no-sync python perf_benchmark/mem_profile.py
    uv run --no-sync python perf_benchmark/mem_profile.py --sessions 3 --tabs 5 --churn 10
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

REPO_ROOT = Path(__file__).resolve().parents[1]
# Reuse the e2e harness (spawns the server exactly like the test suite does).
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "test" / "e2e"))

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402
from mcp_harness import McpServerHarness  # noqa: E402

# Allow-all read policy. Note: browden accepts only https:// by default; a plain
# http host must be named EXPLICITLY in website_overrides (a blanket "*" does NOT
# re-enable http), so 127.0.0.1 is listed on its own to permit the local page.
# Tranco is off, so no snapshot is needed.
BENCH_ALLOWLIST = """\
denylist: {{}}
read:
  enabled: true
  tranco:
    enabled: false
    top_n: 1
  website_overrides:
    "*": [".*"]
    "127.0.0.1": [".*"]
infra:
  max_browser_sessions: {max_sessions}
  max_tabs_per_session: {max_tabs}
"""

# Tiny page with a few elements so DOM queries have something to serialize.
PAGE_HTML = (
    b"<!doctype html><html><head><title>bench</title></head><body>"
    + b"".join(f'<div class="row" id="r{i}">row {i}</div>'.encode() for i in range(40))
    + b"</body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE_HTML)))
        self.end_headers()
        self.wfile.write(PAGE_HTML)

    def log_message(self, *_):  # silence request logging
        pass


# --------------------------------------------------------------------------- #
# Sampler
# --------------------------------------------------------------------------- #
@dataclass
class Sampler:
    """Samples one process (not its children) on a background thread."""
    pid: int
    interval: float = 0.5
    phase: str = "startup"
    _rows: list[dict] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _t0: float = 0.0

    def start(self) -> None:
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        proc = psutil.Process(self.pid)
        proc.cpu_percent(None)  # prime the CPU delta
        while not self._stop.is_set():
            try:
                with proc.oneshot():
                    mem = proc.memory_full_info()  # uss needs the full variant
                    self._rows.append({
                        "t": round(time.time() - self._t0, 2),
                        "phase": self.phase,
                        "uss_mb": mem.uss / 1e6,
                        "rss_mb": mem.rss / 1e6,
                        "cpu_pct": proc.cpu_percent(None),
                        "threads": proc.num_threads(),
                        "fds": proc.num_fds(),
                        "conns": len(proc.net_connections(kind="inet")),
                    })
            except psutil.NoSuchProcess:
                break
            self._stop.wait(self.interval)

    # -- reporting ---------------------------------------------------------- #
    def write_csv(self, path: Path) -> None:
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(self._rows[0].keys()))
            w.writeheader()
            w.writerows(self._rows)

    def summarize(self, phase_order: list[str]) -> None:
        def med(rows, key):
            return statistics.median(r[key] for r in rows) if rows else float("nan")

        by_phase = {p: [r for r in self._rows if r["phase"] == p] for p in phase_order}
        idle = by_phase.get("idle") or []
        idle_uss = med(idle, "uss_mb") if idle else float("nan")

        print(f"\n{'phase':<16}{'n':>4}{'uss_mb':>9}{'Δidle':>8}"
              f"{'rss_mb':>9}{'fds':>6}{'thr':>5}{'cpu_max':>9}")
        print("-" * 66)
        for p in phase_order:
            rows = by_phase.get(p) or []
            if not rows:
                continue
            uss = med(rows, "uss_mb")
            print(f"{p:<16}{len(rows):>4}{uss:>9.1f}{uss - idle_uss:>+8.1f}"
                  f"{med(rows, 'rss_mb'):>9.1f}{med(rows, 'fds'):>6.0f}"
                  f"{med(rows, 'threads'):>5.0f}"
                  f"{max(r['cpu_pct'] for r in rows):>9.1f}")
        print("-" * 66)
        settle = by_phase.get("settle") or []
        if settle and idle:
            residual = med(settle, "uss_mb") - idle_uss
            fd_res = med(settle, "fds") - med(idle, "fds")
            print(f"teardown residual vs idle:  USS {residual:+.1f} MB   FDs {fd_res:+.0f}")
            print("(a large positive residual = Python-side retention worth a memray look)")


# --------------------------------------------------------------------------- #
# Workload driver
# --------------------------------------------------------------------------- #
def _tab_id(result) -> str:
    return json.loads(result.content[0].text)["id"]


async def _hold(sampler: Sampler, phase: str, seconds: float) -> None:
    sampler.phase = phase
    await asyncio.sleep(seconds)


async def drive(url: str, page_url: str, sampler: Sampler, args) -> None:
    async with sse_client(url) as streams:
        async with ClientSession(*streams) as s:
            await s.initialize()

            # 1) idle baseline (server up, nothing opened yet)
            await _hold(sampler, "idle", args.hold)

            # 2) one session per distinct profile_dir -> per-session cost
            sessions = [str(Path(tempfile.gettempdir()) / f"browden-bench-p{i}")
                        for i in range(args.sessions)]
            ids: list[str] = []
            sampler.phase = "sessions"
            for prof in sessions:
                ids.append(_tab_id(await s.call_tool("new_blank_tab", {"profile_dir": prof})))
            await _hold(sampler, "sessions", args.hold)

            # 3) extra tabs in the first session -> per-tab cost
            sampler.phase = "tabs"
            for _ in range(args.tabs):
                ids.append(_tab_id(await s.call_tool("new_blank_tab", {"profile_dir": sessions[0]})))
            await _hold(sampler, "tabs", args.hold)

            # 4) navigate every open tab to the local page
            sampler.phase = "navigate"
            nav_errors = 0
            for tid in ids:
                r = await s.call_tool("navigate", {"url": page_url, "id": tid})
                nav_errors += bool(getattr(r, "isError", False))
            await _hold(sampler, "navigate", args.hold)

            # 5) screenshot every tab (image bytes transit the Python process)
            sampler.phase = "screenshot"
            for tid in ids:
                await s.call_tool("screenshot", {"id": tid})
            await _hold(sampler, "screenshot", args.hold)

            # 6) DOM query every tab (result payload transits Python)
            sampler.phase = "dom"
            dom_found = 0
            for tid in ids:
                r = await s.call_tool("query_selector_all", {"css_selector": ".row", "id": tid})
                try:
                    dom_found += json.loads(r.content[0].text).get("total_count", 0)
                except Exception:
                    pass
            await _hold(sampler, "dom", args.hold)

            # Prove the workload actually exercised the server rather than
            # silently erroring while Chrome sessions merely spawned.
            print(f"workload check: {len(ids)} tabs open, nav_errors={nav_errors}, "
                  f"dom_elements_found={dom_found} (expect ~{40 * len(ids)})")

            # 7) close every tab (ends each session when its last tab closes)
            sampler.phase = "close"
            for tid in ids:
                await s.call_tool("close_tab", {"id": tid})
            await _hold(sampler, "close", args.hold)

            # 8) settle -> compare against idle for residual retention
            await _hold(sampler, "settle", args.hold)

            # 9) churn: open->navigate->screenshot->close, repeatedly, to expose
            #    FD/USS drift across session lifecycles
            if args.churn:
                sampler.phase = "churn"
                prof = str(Path(tempfile.gettempdir()) / "browden-bench-churn")
                for _ in range(args.churn):
                    tid = _tab_id(await s.call_tool("new_blank_tab", {"profile_dir": prof}))
                    await s.call_tool("navigate", {"url": page_url, "id": tid})
                    await s.call_tool("screenshot", {"id": tid})
                    await s.call_tool("close_tab", {"id": tid})
                await _hold(sampler, "churn_settle", args.hold)


PHASE_ORDER = ["idle", "sessions", "tabs", "navigate", "screenshot",
               "dom", "close", "settle", "churn", "churn_settle"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=3, help="distinct Chrome sessions to open")
    ap.add_argument("--tabs", type=int, default=5, help="extra tabs in the first session")
    ap.add_argument("--churn", type=int, default=8, help="open/close churn iterations (0 to skip)")
    ap.add_argument("--hold", type=float, default=6.0, help="seconds to hold each phase")
    ap.add_argument("--interval", type=float, default=0.5, help="sampler interval seconds")
    ap.add_argument("--csv", default=str(Path(__file__).parent / "mem_samples.csv"))
    args = ap.parse_args()

    # Local hermetic page server.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{httpd.server_address[1]}/"

    tmp = Path(tempfile.mkdtemp(prefix="browden-bench-"))
    allowlist = tmp / "allowlist.yaml"
    allowlist.write_text(BENCH_ALLOWLIST.format(
        max_sessions=max(args.sessions + 2, 10),
        max_tabs=max(args.tabs + 5, 20)))

    harness = McpServerHarness(tmp, allowlist_path=allowlist)
    # Silence the refresher's poll so idle CPU reflects only the server at rest.
    import os
    os.environ["BROWDEN_RELOAD_INTERVAL"] = "3600"
    harness.start()
    pid = harness._proc.pid
    print(f"server pid={pid}  url={harness.url}  page={page_url}")
    print(f"policy: allow-all, tranco off | sessions={args.sessions} tabs={args.tabs} "
          f"churn={args.churn} hold={args.hold}s")

    sampler = Sampler(pid=pid, interval=args.interval)
    sampler.start()
    try:
        asyncio.run(drive(harness.url, page_url, sampler, args))
    finally:
        sampler.stop()
        harness.stop()
        httpd.shutdown()

    sampler.write_csv(Path(args.csv))
    sampler.summarize(PHASE_ORDER)
    print(f"\nraw samples -> {args.csv}")


if __name__ == "__main__":
    main()
