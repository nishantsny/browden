# browden MCP server — resource benchmark results

Python-server process only; Chrome/chromedriver excluded. Regenerate with `perf_benchmark/mem_profile.py` (see the README).

- **Generated:** 2026-07-19 04:46:56 UTC
- **Host:** buddy-Virtual-Machine  ·  Linux 6.17.0-35-generic  ·  Python 3.12.3

## Machine

| characteristic | value |
| --- | --- |
| Processor | Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz |
| Architecture | x86_64 |
| Cores (physical / logical) | 2 / 4 |
| Max CPU frequency | n/a |
| Total memory | 13.3 GiB |
| Memory type | Unknown — Hyper-V VM; `dmidecode` reports Type/Speed as Unknown even with root (2 slots, ~13.7 GB) |

## Run

Policy: allow-all, Tranco off. Config: `--sessions 2 --tabs 3 --churn 3 --hold 3.0s --interval 0.5s`.

Workload check: 5 tabs open, nav_errors=0, dom_elements_found=200 (expected ~200).

## Results (median per phase)

| phase | n | USS MB | Δ idle MB | RSS MB | FDs | threads | CPU max % |
| --- | --: | --: | --: | --: | --: | --: | --: |
| idle | 6 | 152.8 | +0.0 | 169.7 | 9 | 1 | 2.0 |
| sessions | 9 | 154.0 | +1.2 | 172.0 | 13 | 2 | 9.8 |
| tabs | 7 | 154.0 | +1.2 | 172.0 | 13 | 2 | 5.9 |
| navigate | 42 | 154.0 | +1.2 | 172.0 | 12 | 2 | 5.9 |
| screenshot | 7 | 154.5 | +1.7 | 172.5 | 13 | 2 | 9.9 |
| dom | 7 | 154.5 | +1.7 | 172.5 | 13 | 2 | 9.9 |
| close | 6 | 154.5 | +1.7 | 172.5 | 13 | 2 | 5.9 |
| settle | 6 | 154.5 | +1.7 | 172.5 | 13 | 2 | 0.0 |
| churn | 52 | 154.5 | +1.7 | 172.5 | 14 | 2 | 11.9 |
| churn_settle | 6 | 154.6 | +1.8 | 172.5 | 15 | 2 | 4.0 |

**Teardown residual vs idle:** USS +1.7 MB, FDs +4 — a large positive residual points at Python-side retention (session bookkeeping / soup cache / sockets), worth a `memray` look. See the README's Limitations.
