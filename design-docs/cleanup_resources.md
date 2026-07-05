# Cleaning up resources

How browser-guard keeps its in-process state and the underlying Chrome session
from drifting out of sync with reality, and how the cleanup work and the
WebDriver session stay out of each other's way — without a lock.

## Why cleanup is needed

browser-guard wraps a long-lived, **shared** Chrome session (the human is using
the same browser, with a persistent profile). Three things accumulate or go
stale on their own if nothing tidies them:

- **Open tabs.** Every `navigate` / `new_blank_tab` leaves a tab open. An agent that
  reads a tab and moves on would otherwise leak tabs for the life of the
  process.
- **Parsed-HTML caches.** Each DOM-query tool parses `driver.page_source` once
  and caches the BeautifulSoup tree for the tab; the cache must be busted when
  the tab changes, expire when it gets old, and not outlive the tab itself —
  otherwise queries return stale content or hold memory for tabs that are gone.
- **Last-access registry entries.** The registry that drives idle-kill must
  reflect *currently open* tabs; a handle the human closed in Chrome (or that
  died with the session) must not linger as a tracked-but-dead id.

The cleanup machinery keeps each of these bounded in size and honest about what
Chrome actually has open.

## What gets cleaned up, and what tracks it

Two state holders alongside the backend, both pure and synchronous, both with an
injectable clock:

- **`TabRegistry`** (`web_navigator/registry.py`) — `tab_id → last_access`,
  using `time.monotonic()` (a *duration*, so a wall-clock jump can't trigger a
  spurious reap). Every tool `touch()`es the tab it acted on; cleanup `forget()`s
  it. `tracked_ids()` lists what we currently track; `idle_tabs(ttl)` returns
  what's gone untouched for at least `ttl`.
- **`SoupCache`** (`web_navigator/soup_cache.py`) — `tab_id → (parsed soup,
  fetched_at)`; 1-hour TTL. `get_soup` returns the cached tree if it's fresh,
  otherwise reloads the tab in the browser and re-parses (`reloaded=True` so the
  caller knows). `invalidate(pid)` drops one entry; `force_reload(pid, backend)`
  reloads + re-parses on demand.

The **backend** is the third resource: it owns the Chrome window handles, and
cleanup ultimately calls `backend.close_tab(pid)` to actually close a tab.

## Cleanup steps — where each one fires

There are three triggers, layered from most-targeted to most-general.

### 1. Eager, per tool call

The tool that just acted on a tab knows exactly what to clean:

| Tool                              | Cleanup it does                                           |
| --------------------------------- | --------------------------------------------------------- |
| `navigate(url)`                   | invalidate the tab's cache; `touch` the registry          |
| `new_blank_tab(url)`                   | invalidate the new tab's cache (no stale entry); `touch`  |
| `close_tab(tab_id)`             | `backend.close_tab`; invalidate cache; `forget` registry |
| `select_tab(tab_id)`            | `touch` the registry                                      |
| `list_tabs()`                    | `touch` every returned tab                                |
| `force_reload_tab(tab_id)`      | reload the tab in the browser; re-parse + store; `touch`  |
| any DOM-query tool                | `get_soup` (which auto-reloads if stale); `touch`         |

So a busy server keeps its state tidy on its own traffic alone.

### 2. Dead-handle eviction

A `tab_id` is a Chrome window handle, and the human shares the browser — they
can close that tab, or click a different one, at any time. Every tool that acts
on a specific tab — `navigate`, `select_tab`, `close_tab`, `force_reload_tab`,
and the four DOM-query tools — therefore takes a **required `tab_id`** (no
"active tab" default): an implicit default would let a human's click silently
redirect the call to the wrong tab and return / modify the wrong tab with no
error — the worst failure mode. Only `list_tabs` and `new_blank_tab` are
tab_id-free, because they don't act on an existing specific tab.

When a backend method is given a handle Chrome no longer knows, Selenium raises
`NoSuchWindowException` (which stringifies to a multi-line driver dump). The
backend translates that to a domain `TabNotFoundError` with a one-line
message; `BrowserSessionManager` catches it, drops the tab from the cache and the
registry, and the DOM tools return
`{"error": "tab <id> is no longer open …", "tab_id": <id>}` (mirroring the
invalid-CSS error shape). `close_tab` on a gone tab is a no-op success;
`select_tab` re-raises (you can't focus a tab that isn't there).

One subtlety worth knowing: a query that's served from a **fresh cached soup
never touches the driver**, so it can't notice the tab is gone — it returns the
cached snapshot. The dead-handle error surfaces on the next backend hit (cache
miss, TTL-expired stale-reload, or `force_reload_tab`). That's the cache
behaving as designed: a deliberate snapshot, not a live view.

### 3. `sweep_idle` — reconcile, then reap

For everything the per-tool path misses — the tab the agent opened and never
touched again; the tab the human closed without the agent ever referencing it
afterwards — there's `BrowserSessionManager.sweep_idle()`:

1. **Reconcile.** `live = backend.list_tab_ids()` (a cheap call — just the
   handles, no per-tab focus changes; distinct from the heavy `list_tabs()`).
   If that call raises, skip this step — best-effort. Otherwise, for every
   `pid` the registry tracks that isn't in `live`: `cache.invalidate(pid)` +
   `registry.forget(pid)`. This catches tabs the human closed *without* the
   agent touching them again.
2. **Reap the idle.** For each `pid` in `registry.idle_tabs(IDLE_TTL_SECONDS)`
   (1 hour by default), call `backend.close_tab(pid)` (swallow any error — the
   last-tab `ValueError`, an already-closed tab, a dead session), then
   `cache.invalidate(pid)` + `registry.forget(pid)`.

The "last remaining tab" is never closed — closing the only window quits the
driver, which is pointless — but its cache and registry entries are still
dropped, so it stops being tracked until something touches it again.

`sweep_idle` is called from two places:

- **Lazily**, as the first line of every tool coroutine. Cheap (usually a
  no-op), and a busy server reaps on its own traffic.
- **Periodically**, by a fire-and-forget reaper task —
  `asyncio.create_task(_reaper_loop())`, where `_reaper_loop` is
  `while True: await asyncio.sleep(REAP_INTERVAL_SECONDS); sweep_idle()`. This
  covers an *idle* server, which by definition gets no tool calls. The task is
  created the first time the session is built (inside a tool call, so a loop is
  already running) — never at import. There's no clean-shutdown path: it's a
  daemon-ish coroutine, there's no thread to join, and the process exit takes it
  down with everything else.

`REAP_INTERVAL_SECONDS` (5 min) just bounds how late a reap can be; the TTL is
what matters. A closed tab is dropped from tracking by whichever happens first:
a tool that touches it again (immediate, via `TabNotFoundError`), the next
reconcile (within `REAP_INTERVAL_SECONDS`), or the idle TTL itself.

## Concurrency challenges & resolution

Cleanup runs in the same process as the tools, against the same WebDriver
session. That creates two distinct hazards:

- **(a) Reaper task vs an in-flight tool's driver op.** Selenium is synchronous
  and not thread-safe — two threads can't hold the same WebDriver session at
  once. So a reaper tick that fires *while* a tool's `close_tab` / `navigate` /
  tab-source fetch is in flight must not issue its own `close_tab` /
  `list_tab_ids` against that same driver.
- **(b) Two tool coroutines reaching driver ops at once.** Same hazard,
  different source.

The design goal was **no `threading` mutex** — concurrency is handled by
cooperative scheduling plus one boolean.

### How the pieces line up

The server is async (FastMCP already runs an asyncio event loop). One loop, one
thread for everything that isn't a WebDriver call.

**The pure work is atomic for free.** Cache lookup/invalidate, registry
touch/forget, running a query, serializing nodes, and *all of* `sweep_idle`
itself run synchronously on the loop thread. asyncio can't preempt a coroutine
mid-function, so none of that can interleave with another coroutine.

**WebDriver calls go through `asyncio.to_thread`.** Every backend call is
dispatched via `BrowserSessionManager._run_driver`:

```python
async def _run_driver(self, fn, *args, **kwargs):
    self._driver_busy = True
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    finally:
        self._driver_busy = False
```

`_driver_busy` is assigned **on the loop thread**, immediately before and after
the `await` — never inside the worker thread. While a driver op is parked in
`to_thread`, the loop is free, so other coroutines (including the reaper's
`asyncio.sleep` waking up) can still run.

**`sweep_idle` short-circuits on the flag.** Its first line is
`if self._driver_busy: return`. So if the reaper tick fires while a tool's
driver op is in flight, the sweep just bails and tries again next interval — it
can never issue a WebDriver command concurrently with the in-flight one. That
resolves hazard **(a)**: the race an `RLock` around the driver would have
guarded is closed by the flag plus cooperative scheduling.

When `sweep_idle` *does* proceed, its driver calls (`list_tab_ids` for the
reconcile, `close_tab` for each idle tab) run synchronously on the loop thread
and briefly block it. That's accepted: a driver op blocks *something*; here it's
the loop instead of a worker thread, it's bounded, `list_tab_ids` is cheap, and
the `close_tab` loop only runs when there's actually an idle tab to close.

### Hazard (b): the gap, and why it's fine

The flag does *not* cover two **tool** coroutines both reaching
`await asyncio.to_thread(<driver op>)` at the same time. We rely on the
**serial MCP stdio client**: it sends one request and waits for the response
before sending the next, so two tool coroutines are never in flight together.
This is the same assumption the original synchronous tools always made. If a
concurrent client ever mattered, an `asyncio.Lock` around the `to_thread`
section would close the gap — but that's a lock, and out of scope here.

## Where the pieces live

```
mcp/server.py                     async @mcp.tool() fns; lazily builds the session
web_navigator/session.py          BrowserSessionManager — async coordinator:
  ├─ _backend  : WebNavigatorBackend   (synchronous; the only WebDriver code)
  ├─ _cache    : SoupCache             (1h DOM cache)
  ├─ _registry : TabRegistry          (last-access times)
  ├─ _driver_busy : bool               (set around every to_thread driver op)
  └─ _reaper_task : asyncio.Task       (periodic sweep_idle loop)
web_navigator/soup_cache.py       SoupCache, TTL_SECONDS
web_navigator/registry.py         TabRegistry
web_navigator/interface.py        WebNavigatorBackend, TabNotFoundError;
                                  list_tab_ids() is the cheap "what tabs exist"
```

Tunables (`session.py` / `soup_cache.py`): `IDLE_TTL_SECONDS = 3600`,
`REAP_INTERVAL_SECONDS = 300`, `TTL_SECONDS = 3600` (DOM cache).
