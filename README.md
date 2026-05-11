# browser-guard

A protective MCP shell around browser automation. Exposes a small, audited
surface to an LLM agent so it can drive a real Chrome session without being
handed the full power of a CDP or Playwright client.

## Capabilities

Five tools, mapped to a swappable `WebNavigatorBackend`:

| Tool          | Purpose                                              |
| ------------- | ---------------------------------------------------- |
| `list_pages`  | List all open tabs.                                  |
| `new_page`    | Open a new tab, optionally at a URL.                 |
| `close_page`  | Close a tab by id (refuses the last one).            |
| `select_page` | Switch the active tab.                               |
| `navigate`    | Navigate the active tab to a URL.                    |

Default backend is `SeleniumChromeBackend` using a persistent Chrome profile
at `~/.cache/browser-guard/chrome-profile`, so logins survive restarts. The
backend self-heals after a dead Chrome session and clears stale
`Singleton{Lock,Cookie,Socket}` files left by unclean shutdowns.

## Restrictions

`navigate()` and `new_page(url=…)` run every URL through `validate_url`:

- Bare domains are normalized to `https://`.
- Paths are allowed (`amazon.com/orders` ✓).
- **Query strings are rejected** (`amazon.com/orders?ref=foo` ✗).
- **Fragments are rejected** (`amazon.com/page#section` ✗).
- A `netloc` is required.

Failures raise `ValidationError`, which FastMCP surfaces as a structured
tool error.

## Adding to an LLM agent

Add an entry to your `~/.claude.json` `mcpServers` block:

```json
"browser-guard": {
  "command": "/path/to/browser-guard/.venv/bin/python",
  "args": ["-m", "browser_guard.mcp.server"],
  "env": { "DISPLAY": ":0" }
}
```

`DISPLAY` is only needed when launching headed Chrome from a non-graphical
parent process (e.g. an MCP server spawned by Claude Code). Restart the
agent to register the server.

## Dependencies

- Python ≥ 3.11
- [`mcp[cli]`](https://pypi.org/project/mcp/) ≥ 1.0 — FastMCP server SDK
- [`selenium`](https://pypi.org/project/selenium/) ≥ 4.20 — bundles
  Selenium Manager, so ChromeDriver is auto-downloaded
- Google Chrome installed on the host
- `pytest` (dev only)

Install:

```bash
uv venv && uv pip install -e ".[dev]"
pytest test/unit/
```

## Layout

```
browser_guard/
├── common/          # PageInfo dataclass
├── dependencies/    # anti-corruption wrappers around mcp + selenium
├── mcp/             # FastMCP server + URL validator
└── web_navigator/   # WebNavigatorBackend interface + selenium_chrome backend
```

`web_navigator/` never imports from `mcp/`; backends are the only place
third-party browser libraries are touched. Swap the backend by changing one
line in `mcp/server.py`.

## License

[Apache License 2.0](./LICENSE).
