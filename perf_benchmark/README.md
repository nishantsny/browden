# perf_benchmark

Resource profiling for the browden MCP **Python server process only**.

Chrome and chromedriver run as separate child PIDs and are **not** measured —
the sampler reads exactly the `python -m browden.mcp.server` process, so the
numbers reflect the server's own footprint (selenium client objects, session
bookkeeping, cached DOM/screenshot payloads, chromedriver sockets, asyncio).

## Run

```bash
uv run --no-sync python perf_benchmark/mem_profile.py
# knobs:
uv run --no-sync python perf_benchmark/mem_profile.py --sessions 3 --tabs 5 --churn 8 --hold 6
```

Requires `psutil` and a working headless Chrome (same prerequisites as the e2e
suite). It spawns its own server on a free port under an allow-all, Tranco-off
policy and serves a local page, so it needs no external network and won't touch
any `~/.browden` config.

## Output

- A per-phase table: median USS/RSS/FDs/threads, USS delta vs the idle baseline,
  and peak CPU per phase.
- `teardown residual vs idle`: how far USS/FDs sit above baseline after every tab
  and session is closed — a large positive residual points at Python-side
  retention.
- `perf_benchmark/mem_samples.csv`: every raw sample (`t, phase, uss_mb, rss_mb,
  cpu_pct, threads, fds, conns`) for your own plotting. Gitignored (per-run).
- `perf_benchmark/results/<UTC-date>_<hostname>.md`: a **committed**, human-readable
  results file — the run date, the machine's characteristics (processor, arch,
  core count, total memory, and memory type when `dmidecode` is available), the
  run config, the workload self-check, and the per-phase table. One file per
  date+host, so results from different machines sit side by side and stay
  comparable over time. Memory type and CPU max-frequency are best-effort:
  they read `dmidecode` (needs root) and `psutil.cpu_freq()`, and print a clear
  "unavailable/n-a" on hosts (e.g. VMs) that don't expose them.

## Phases

`idle → sessions → tabs → navigate → screenshot → dom → close → settle → churn`

`sessions` opens one Chrome session per distinct `profile_dir` (per-session cost);
`tabs` adds tabs to the first session (per-tab cost); `churn` repeats
open→navigate→screenshot→close to expose FD/USS drift across session lifecycles.

## Coverage — what it measures

- **Server-process footprint, Chrome excluded:** single-PID USS (headline), RSS,
  CPU%, thread count, FD count, inet connection count.
- **Scaling coefficients:** Δ USS/FDs **per Chrome session** (distinct
  `profile_dir`) and **per tab**.
- **Per-operation cost:** navigate, screenshot (image bytes transit Python), and
  DOM query (`query_selector_all`) payloads.
- **Leak signal:** a churn loop (open→navigate→screenshot→close ×N) to expose
  USS/FD drift across session lifecycles, plus a `settle` phase measuring
  residual USS/FDs after every tab is closed vs. the idle baseline.
- **Real transport path:** load goes through `sse_client` +
  `ClientSession.call_tool`, not a mock.
- **Workload self-check:** prints `nav_errors` and DOM elements found, so a
  silently-erroring run can't masquerade as a low-memory result.

## Limitations — read before trusting a number

- **Sampling, not accounting.** ~2 Hz psutil can miss sub-second allocation
  spikes. This tells you *how much*, not *what line* — use
  `memray run -m browden.mcp.server …` for allocation attribution.
- **USS is a coarse `/proc/smaps` snapshot;** treat deltas under ~3 MB as noise.
- **Allocator retention ≠ leak.** Python/glibc may hold freed pages, so a
  non-zero teardown residual is a signal to investigate (ideally via memray),
  not proof of a bug.
- **"Python only" still includes selenium's client objects + urllib3 socket
  pool** per session — legitimately the server's cost, but coupled to the
  selenium version, not just browden's code.
- **Single host, single run.** No cross-run percentiles, no sustained-RPS or
  latency load testing — it's a profiler, not a load-test rig.
- **Idle CPU floor** is suppressed here by setting `BROWDEN_RELOAD_INTERVAL=3600`
  (the allowlist refresher poll); real deployments tick every 10s.
- **Requires headless Chrome** (same prerequisites as the e2e suite) and
  `psutil`.
