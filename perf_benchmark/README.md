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
  cpu_pct, threads, fds, conns`) for your own plotting.

## Phases

`idle → sessions → tabs → navigate → screenshot → dom → close → settle → churn`

`sessions` opens one Chrome session per distinct `profile_dir` (per-session cost);
`tabs` adds tabs to the first session (per-tab cost); `churn` repeats
open→navigate→screenshot→close to expose FD/USS drift across session lifecycles.

## What this is and isn't

- **Is:** a single-PID sampling profiler for *how much* the server process costs
  under a scripted workload, and whether it returns to baseline after teardown.
- **Isn't:** allocation attribution (use `memray run -m browden.mcp.server …`
  for *what* allocates), and not a throughput/latency load-test rig.

Sampling at ~2 Hz can miss sub-second spikes; treat USS deltas under ~3 MB as
noise (USS is a coarse `/proc/smaps` snapshot).
