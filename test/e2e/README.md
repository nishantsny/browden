# End-to-end test suite

Every test here drives **real headless Chrome** (and, for the server-level
tests, a **real MCP server subprocess**), so the whole chain runs the way the
deployed service does. The deploy smoke test (`~/scripts/deploy.sh`) runs this
entire directory (`pytest test/e2e`) after every restart, so anything added here
is automatically part of the post-deploy gate.

Run locally with headless Chrome:

```bash
BROWDEN_HEADLESS=1 pytest test/e2e -q
```

All tests are **offline and deterministic**: pages are inline `data:` documents,
a temp `file://`, or an in-process `http.server`; gate refusals happen in
`validate_url` before any load, and the few "allowed" made-up hosts merely fail
DNS (a navigation outcome, never a gate refusal).

## Test inventory

| File | Layer | What it covers |
| --- | --- | --- |
| `test_tabs.py` | backend | navigate / select_tab / list_tabs / close_tab on `data:` pages |
| `test_navigation.py` | backend | new_blank_tab + list_tabs (a small subset of `test_tabs.py`) |
| `test_screenshot.py` | backend | screenshot returns real PNG bytes; defaults to active tab |
| `test_dom_queries.py` | session-mgr | get_element_by_id / class / query_selector(_all) pagination / invalid-CSS / force_reload |
| `test_insert_text.py` | session-mgr | write-text: clear+replace input & textarea; ambiguous-selector refusal |
| `test_anchor_click.py` | session-mgr | click an `<a>` fires its onclick; reveals a label-less field, then insert_text |
| `test_concurrent_profiles.py` | backend | distinct profiles run concurrently & are isolated |
| `test_mcp_concurrent_profiles.py` | MCP server | two MCP clients / two profiles: isolation, id namespacing, routing |
| `test_mcp_endpoint.py` | MCP server | tool list; new_blank_tab/list/query; **data: navigate refused**; screenshot; close |
| `test_resource_caps.py` | MCP server | per-session tab cap & browser-session cap (trip + recover) |
| `test_tab_cap_reclaim.py` | session-mgr | hitting the tab cap sweeps idle tabs and retries; refuses (sacrificing nothing) when all tabs are in use |
| `test_idle_reaper.py` | session-mgr | **the reaper's timer**: one tick at the configured interval really closes an idle tab in Chrome and spares a used one; nothing is reaped before a tick lands |
| `test_psl_read_gate.py` | MCP server | **PSL** host reduction: public-suffix subdomain & lookalike refused; listed registrable domain allowed (H1/H3) |
| `test_fetch_tranco_pipeline.py` | setup | fetch_tranco id-resolve → download → parse → gzip → TrancoList membership |
| `test_setup_script.py` | setup | onetime_setup.py service mode + stdio mode |
| `test_read_scheme_gate.py` | MCP server | **https-only** default; **file://** opt-in read; **localhost** dev-server read; **about:blank** readable under deny-all; **denylist** veto |
| `test_hot_reload.py` | MCP server | **live reload**: an allowlist edit takes effect with no restart; a broken edit keeps the last-good policy |

## Notes on duplication

- `test_navigation.py` is a strict subset of `test_tabs.py`; kept deliberately
  (erring toward more coverage), not a true duplicate to remove.
- `test_concurrent_profiles.py` (backend) and `test_mcp_concurrent_profiles.py`
  (full MCP server) look similar but exercise **different layers** — both stay.
