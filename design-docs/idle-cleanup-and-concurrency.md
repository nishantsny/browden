# Idle-tab cleanup & concurrency

How browser-guard closes stale tabs, and how the cleanup task and the
WebDriver session are kept from stepping on each other — without a lock.

## What needs cleaning up, and why

The server drives a long-lived Chrome session. Every `navigate`, `new_page`, or
DOM query against a tab leaves that tab open; nothing closes it. An agent that
opens a tab, reads it, and moves on would otherwise leak tabs for the life of
the process. So: **a tab that goes `IDLE_TTL_SECONDS` (1 hour) without a DOM
query or a navigation is closed and dropped from tracking.**

Two state holders, both pure and synchronous, both with an injectable clock:

- **`PageRegistry`** (`web_navigator/registry.py`) — `page_id → last_access`,
  using `time.monotonic()` (a duration, so a wall-clock jump can't trigger a
  spurious reap). Every tool `touch()`es the tab it acted on.
- **`SoupCache`** (`web_navigator/soup_cache.py`) — `page_id → (parsed soup,
  fetched_at)`, the 1-hour DOM cache. Reaping a tab also drops its cache entry.

The "last remaining tab" is never closed — closing the only window quits the
driver, which is pointless — but its cache and registry entries are still
dropped, so it stops being tracked until something touches it again.

## Two triggers: lazy sweep + periodic reaper

`PageSession.sweep_idle()` does the work: for each `pid` in
`registry.idle_pages(IDLE_TTL_SECONDS)`, call `backend.close_page(pid)` (swallow
any error — the last-tab `ValueError`, an already-closed tab, a dead session),
then `cache.invalidate(pid)` and `registry.forget(pid)`.

It's called from two places:

1. **Lazily**, as the first line of every tool coroutine. Cheap, and it means a
   busy server reaps on its own traffic.
2. **Periodically**, by a fire-and-forget reaper task —
   `asyncio.create_task(_reaper_loop())`, where `_reaper_loop` is
   `while True: await asyncio.sleep(REAP_INTERVAL_SECONDS); sweep_idle()`. This
   covers an *idle* server, which by definition gets no tool calls. The task is
   created the first time the session is built (inside a tool call, so an event
   loop is already running) — never at import. There's no clean-shutdown path:
   it's a daemon-ish coroutine, there's no thread to join, and the process exit
   takes it down with everything else.

`REAP_INTERVAL_SECONDS` (5 min) just bounds how late a reap can be; the TTL is
what matters.

## Concurrency: one loop, one busy flag, no lock

The server is async (FastMCP already runs an asyncio event loop). The design
goal was **no `threading` mutex** — concurrency is handled by cooperative
scheduling plus one boolean.

**The pure work is atomic for free.** Cache lookup/invalidate, registry
touch/forget, running a query, serializing nodes, and *all of* `sweep_idle`
itself run synchronously on the loop thread. asyncio can't preempt a coroutine
mid-function, so none of that can interleave with another coroutine.

**WebDriver calls go through `asyncio.to_thread`.** Selenium is synchronous and
not thread-safe, so the one thing that *can't* just run on the loop thread is a
WebDriver command. Every backend call is dispatched via
`PageSession._run_driver`:

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
`asyncio.sleep` waking up) can run.

**`sweep_idle` short-circuits on the flag.** Its first line is
`if self._driver_busy: return`. So if a reaper tick fires while a tool's driver
op is in flight, the sweep just bails and tries again next interval — it can
never issue a WebDriver command concurrently with the in-flight one. (When
`sweep_idle` *does* proceed, its `close_page` calls run synchronously on the
loop thread and briefly block it. That's accepted: a driver op blocks
*something*; here it's the loop instead of a worker thread, it's bounded, and it
only happens when there's actually an idle tab to close.)

This is exactly the race an `RLock` around the driver would have guarded; the
flag plus cooperative scheduling replace it.

**The one gap, and why it's fine.** The flag covers reaper-vs-tool. It does
*not* cover two *tool* coroutines both reaching `await asyncio.to_thread(<driver
op>)` at the same time — that would be two threads in the WebDriver session at
once, which is unsafe. We rely on the **serial MCP stdio client**: it sends one
request and waits for the response before sending the next, so two tool
coroutines are never in flight together. This is the same assumption the
original five synchronous tools always made. If a concurrent client ever
mattered, an `asyncio.Lock` around the `to_thread` section would close the gap —
but that's a lock, and out of scope here.

## Tab identity & dead handles

A `page_id` is a Chrome window handle, and the human shares the browser — they
can close that tab, or click a different one, at any time. Two consequences:

- **No implicit "active tab" for reads.** `navigate` / `select_page` act on
  whatever's focused (that's the point of them), but the four DOM-query tools
  take a **required `page_id`**. An implicit "active tab" default would let a
  human's click silently redirect a query to the wrong page and return wrong
  data with no error — the worst failure mode. Callers always have the id in
  hand (`new_page` / `navigate` / `list_pages` return it).
- **A dead handle is a clean error, not a stack trace.** When a backend method
  is given a handle Chrome no longer knows, Selenium raises
  `NoSuchWindowException` (which stringifies to a multi-line driver dump). The
  backend translates that to `PageNotFoundError` with a one-line message;
  `PageSession` catches it, drops the tab from the cache and registry, and the
  DOM tools return `{"error": "page <id> is no longer open …", "page_id": <id>}`
  (mirroring the invalid-CSS error shape). `close_page` on a gone tab is treated
  as a no-op success; `select_page` re-raises (you can't focus a tab that isn't
  there).

  One subtlety: a query that's served from a **fresh cached soup never touches
  the driver**, so it can't notice the tab is gone — it returns the cached
  snapshot. The dead-handle error surfaces on the next backend hit (cache miss,
  TTL-expired stale-reload, or `force_reload_page`). That's the cache behaving
  as designed: it's a deliberate snapshot, not a live view.

## Where the pieces live

```
mcp/server.py                     async @mcp.tool() fns; lazily builds the session
web_navigator/session.py          PageSession — async coordinator:
  ├─ _backend  : WebNavigatorBackend   (synchronous; the only WebDriver code)
  ├─ _cache    : SoupCache             (1h DOM cache)
  ├─ _registry : PageRegistry          (last-access times)
  ├─ _driver_busy : bool               (set around every to_thread driver op)
  └─ _reaper_task : asyncio.Task        (periodic sweep_idle loop)
web_navigator/soup_cache.py       SoupCache, TTL_SECONDS
web_navigator/registry.py         PageRegistry
```

Tunables (`session.py` / `soup_cache.py`): `IDLE_TTL_SECONDS = 3600`,
`REAP_INTERVAL_SECONDS = 300`, `TTL_SECONDS = 3600` (DOM cache).
