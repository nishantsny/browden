# Package layout

```
browser_guard/
├── common/          # TabInfo dataclass
├── dependencies/    # anti-corruption wrappers around mcp + selenium + bs4
├── dom/             # pure DOM helpers: query primitives + element serialization
├── mcp/             # FastMCP server + URL validator
└── web_navigator/   # WebNavigatorBackend interface, selenium_chrome backend,
                     #   BrowserSessionManager coordinator (soup cache + idle reaper)
design-docs/         # these notes
test/                # unit + e2e tests
```

## Rules between the layers

- **`web_navigator/` never imports from `mcp/`.** The navigator half knows
  nothing about the MCP server that drives it.
- **Backends are the only place third-party browser libraries are touched**
  (`selenium`, and any future `playwright`, …) — and the only synchronous
  WebDriver code. `BrowserSessionManager` (`web_navigator/session.py`) is the async
  coordinator that owns the soup cache and the idle reaper and dispatches every
  backend call off the event loop; see
  [`page_caching.md`](page_caching.md) for the cache contract and
  [`cleanup_resources.md`](cleanup_resources.md) for the cleanup + concurrency model.
- **`dom/` is pure** — it eats an HTML string (the rendered DOM) and returns
  query results / JSON nodes; no Selenium, no I/O, no `mcp/`.
- **Nothing imports a third-party SDK directly** — only via `dependencies/`
  (`mcp.py`, `selenium.py`, `bs4.py`), so the surface we depend on is visible in
  one place and trivial to mock.

Swap the browser backend by changing one line in `mcp/server.py` (the
`SeleniumChromeBackend()` it wraps in a `BrowserSessionManager`).
