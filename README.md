# browser-guard

A protective MCP shell around browser automation. Exposes a small, audited
surface to an LLM agent so it can drive a real Chrome session without being
handed the full power of a CDP or Playwright client.

## Capabilities

Ten tools, mapped to a swappable `WebNavigatorBackend`.

**Tabs**

| Tool          | Purpose                                              |
| ------------- | ---------------------------------------------------- |
| `list_pages`  | List all open tabs.                                  |
| `new_page`    | Open a new tab, optionally at a URL.                 |
| `close_page`  | Close a tab by id (refuses the last one).            |
| `select_page` | Switch the active tab.                               |
| `navigate`    | Navigate a named tab to a URL.                       |

**Reading page content** — mirrors the four browser DOM-query APIs, server-side, over the rendered (post-JS) DOM:

| Tool                          | Mirrors                            |
| ----------------------------- | ---------------------------------- |
| `get_element_by_id`           | `document.getElementById`          |
| `get_elements_by_class_name`  | `document.getElementsByClassName`  |
| `query_selector`              | `document.querySelector`           |
| `query_selector_all`          | `document.querySelectorAll`        |
| `force_reload_page`           | reload a tab + refresh its cache   |

Every tool that acts on a specific tab — `navigate`, `select_page`,
`close_page`, `force_reload_page`, and all four DOM queries — takes a
**required `page_id`** (the id returned by `new_page` / `list_pages`). None of
them default to "the active tab", since the active tab is shared state the
human also controls, and an implicit default would silently act on whichever
tab happened to be focused. A `page_id` that no longer names an open tab comes
back as `{"error": …, "page_id": …}` (and that tab is dropped from the cache);
so does an invalid CSS selector — structured errors, not exceptions.

Results are plain JSON "nodes" — `tag`, `id`, `classes`, `attributes`,
collapsed `text`, sizes (`text_length`, `html_length`, `child_count`) — with
attribute values and text truncated to keep responses small (true lengths are
reported; outer HTML is omitted unless you pass `include_html=true`). The list
tools (`get_elements_by_class_name`, `query_selector_all`) are paginated
(`limit` ≤ 50, `offset`, `next_offset`).

**Caching.** The parsed DOM for a tab is cached for one hour. A query against a
tab whose cache has expired transparently reloads that tab in the browser,
re-parses, and tells the caller it did (`reloaded: true`); `navigate` /
`new_page` / `close_page` invalidate the relevant tab's cache; `force_reload_page`
busts it on demand. See [`design-docs/page_caching.md`](design-docs/page_caching.md)
for the snapshot semantics and where they bite.

**Idle-tab cleanup.** A tab that goes one hour without a DOM query or navigation
is closed and dropped from tracking, via a periodic sweep plus a lazy sweep on
every tool call (the last remaining tab is left open). See
[`design-docs/cleanup_resources.md`](design-docs/cleanup_resources.md)
for how the reaper and the WebDriver session stay out of each other's way without
a lock.

Default backend is `SeleniumChromeBackend` using a persistent Chrome profile
at `~/.cache/browser-guard/chrome-profile`, so logins survive restarts. The
backend self-heals after a dead Chrome session and clears stale
`Singleton{Lock,Cookie,Socket}` files left by unclean shutdowns.

## Restrictions

`navigate()` and `new_page(url=…)` run every URL through `validate_url`,
which gates against a per-host allowlist defined in
[`browser_guard/mcp/validator/allowlist.json`](browser_guard/mcp/validator/allowlist.json).

- Bare domains are normalized to `https://`.
- A `netloc` is required.
- The `(host, path)` pair must match a regex listed under that host. The
  host is lower-cased and a leading `www.` is stripped before lookup.
- For URLs that match, query strings, fragments, `&`, and spaces are
  preserved as-is — only the host+path are gated.
- Anything not on the allowlist is rejected.

Failures raise `ValidationError`, which FastMCP surfaces as a structured
tool error. Extend the allowlist by editing `allowlist.json` and adding
the URL shape you actually need — start narrow.

## Running as a Background Service (SSE)

If you want the MCP server to stay active in the background (managed by the OS), you can use the SSE (Server-Sent Events) transport. This keeps the Chrome session "warm" and persistent between agent sessions.

1. **Install Dependencies:**
   ```bash
   git clone https://github.com/nishantsny/browser-guard.git
   cd browser-guard
   uv venv && uv pip install -e .
   ```
2. **Generate the Service File:**
   Ensure your virtual environment is active so the script picks up the correct Python binary, then run the installer:
   ```bash
   python -m browser_guard.scripts.install_service
   ```
   *(Optional: Use `--port 8080`, `--python /custom/bin/python`, or `--display :1` if you need to override the defaults).*
3. **Start the Service:**
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now browser-guard
   ```
4. **Configure Your Agent (e.g., Claude, Gemini, Codex):**
   Add the SSE connection to your agent's configuration file (e.g., `~/.claude.json` or `.gemini/settings.json`). Ensure the port matches the one you configured (8000 is the default):
   ```json
   "mcpServers": {
     "browser-guard": {
       "type": "sse",
       "url": "http://localhost:8000/sse"
     }
   }
   ```

## Adding to an LLM agent (stdio)

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
- [`beautifulsoup4`](https://pypi.org/project/beautifulsoup4/) ≥ 4.12 — DOM parsing for the query tools
- Google Chrome installed on the host
- `pytest`, `pytest-asyncio` (dev only)

Install:

```bash
uv venv && uv pip install -e ".[dev]"
pytest test/unit/
```

## Design docs

- [`design-docs/layout.md`](design-docs/layout.md) — package layout and the import rules between them
- [`design-docs/page_caching.md`](design-docs/page_caching.md) — what the per-tab parsed-DOM cache caches, its TTL / invalidation paths, and the snapshot semantics callers see (`reloaded` flag, dead-tab corner)
- [`design-docs/cleanup_resources.md`](design-docs/cleanup_resources.md) — why and how tabs, parsed-HTML caches, and registry entries get cleaned up, plus the lock-free concurrency model

## End-to-end evals

Agent-driven scenario evals live under [`agentic_evals/evals/`](agentic_evals/evals/) — one Markdown file per scenario, executed against the live MCP. See [`agentic_evals/explanation.md`](agentic_evals/explanation.md) for the conventions and how to add a scenario.

## License

[Apache License 2.0](./LICENSE).
