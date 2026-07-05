# Tab caching

Why the MCP caches a parsed DOM per tab, what semantics the cache gives the
caller, and the failure modes those semantics produce. Cleanup of cache entries
sits inside the broader resource story in
[`cleanup_resources.md`](cleanup_resources.md); this doc is just about the
cache.

## Why cache at all

A DOM-query tool call does two things that aren't free:

- **`driver.page_source`** is a WebDriver round-trip that serializes the live
  DOM in the renderer, marshals it through CDP, and hands back a string. On a
  big SPA (Amazon order history, Gmail, a long Twitter feed) this is hundreds
  of KB to a few MB and takes a noticeable fraction of a second.
- **`BeautifulSoup(html, "html.parser")`** then parses that string. Parse cost
  grows with the document, and the agent typically issues many queries against
  the same tab (find an order card, then read its title, date, price, the
  list of links inside it…).

Without a cache, every `query_selector` / `get_element_by_id` call would pay
both costs. Each tool call is already a model round-trip away from the
previous one, so the agent is unlikely to be racing with itself — there's no
good reason to re-fetch the same DOM between two queries that happen seconds
apart.

So the cache: parse once on first read, reuse for an hour, drop on anything
that says "the tab changed."

## What's in it

`SoupCache` (`web_navigator/soup_cache.py`) is a plain `dict[str, CacheEntry]`:

```python
@dataclass
class CacheEntry:
    soup: BeautifulSoup
    fetched_at: float    # time.monotonic()
```

- **Key** is the `tab_id` — a Chrome window handle, which is what every tool
  threads around. One entry per tab.
- **Value** is the *parsed* tree. We don't keep the raw HTML; `dom/query.py`
  works directly against the soup, and re-parsing on demand defeats the point.
- **Clock** is `time.monotonic` (injectable for tests). TTL is a *duration*,
  not a wall-clock instant, so an NTP step / DST change / suspend-resume can't
  silently flip a fresh entry to stale.

`TTL_SECONDS = 3600` (1 hour). That's a guess at "how long an agent might
still care about a tab it loaded," tuned so the agent doesn't see stale
content past the point a human reload would have happened anyway.

## The three outcomes of `get_soup`

Every DOM-query tool routes through `SoupCache.get_soup(tab_id, backend)`,
which returns `(soup, reloaded: bool)`:

| State on entry                       | What it does                                                      | `reloaded` |
| ------------------------------------ | ----------------------------------------------------------------- | ---------- |
| no entry for `tab_id`               | fetch `page_source`, parse, store with `fetched_at = now`         | `False`    |
| entry present, `now − fetched_at < TTL` | return the cached soup as-is                                   | `False`    |
| entry present, `now − fetched_at ≥ TTL` | `backend.reload(tab_id)`, fetch + parse fresh, overwrite     | `True`     |

The cache-miss branch reports `reloaded=False` deliberately: a tab the agent
just opened (via `navigate` / `new_blank_tab`, which both invalidate) isn't
"reloaded" on its first query — that's the *first* load, semantically equal to
the human typing the URL. `reloaded=True` is reserved for "the cache decided
to ditch what you would have gotten and pull fresh."

That flag is part of the tool's response (`{"reloaded": true/false, …}`) —
it's how the caller knows whether the result is from a snapshot taken
seconds ago or one taken on this call.

## Invalidation: who busts the cache, when

Anything that changes what `driver.page_source` would return must drop the
entry. The map:

| Trigger                                  | Path                                                                     |
| ---------------------------------------- | ------------------------------------------------------------------------ |
| `navigate(url)`                          | `cache.invalidate(tab_id)` after the backend nav succeeds               |
| `new_blank_tab(url)`                          | `cache.invalidate(new_page_id)` (no stale entry from a recycled handle)  |
| `close_tab(tab_id)`                    | `cache.invalidate(tab_id)`                                              |
| `force_reload_tab(tab_id)`             | `cache.force_reload(tab_id, backend)` — reload + re-parse + overwrite   |
| TTL expiry on a query                    | the stale branch above (inside `get_soup` itself)                        |
| dead handle (`TabNotFoundError`)        | `BrowserSessionManager._drop(tab_id)` — invalidate + forget in the registry       |
| idle reap reconcile (human closed a tab) | `sweep_idle` invalidates every tracked id not in `backend.list_tab_ids()` |
| idle reap (1h untouched)                 | `sweep_idle` invalidates after closing the tab                            |

The first four are *eager* — the tool that knows the tab just changed busts
its own entry. The last four are *reconciling* — they fix up entries that
went stale or dead without anything telling the cache.

Note what *doesn't* invalidate: a human navigating the tab in Chrome (typing
a URL into a tab the agent is tracking) doesn't tell the MCP anything. The
cache stays warm with the previous tab's DOM until either the TTL elapses or
the agent calls `force_reload_tab`. Same for client-side route changes in an
SPA — the URL changes, the cached soup doesn't. Both are accepted: an agent
operating against a tab the human is also editing is already in a contested
state, and the snapshot semantics below give it a consistent (if old) view
rather than a flickering one.

## Snapshot semantics, and the dead-tab corner

A fresh cached hit **never touches the driver.** That has a useful
consequence and one weird one:

- **Useful:** repeated queries against the same tab produce the same nodes,
  same counts, same text, even if the tab is mutating in the background.
  An agent enumerating order cards by paginating `query_selector_all` won't
  see an item appear or disappear between tab 0 and tab 1.
- **Weird:** if the human closes that tab in Chrome between the agent's
  queries, a query served from the fresh cache *succeeds with cached
  content*, because nothing on the path hit the driver to notice the tab is
  gone. The dead-handle error only surfaces on the next backend touch — a
  cache miss, a TTL stale-reload, an explicit `force_reload_tab`, or the
  reaper's reconcile (within `REAP_INTERVAL_SECONDS`).

That's the cache behaving as designed: a *snapshot*, not a live view. The
alternative — verifying the handle on every query — would mean a driver
round-trip per query, which is the cost the cache exists to avoid. Agents
that need a guaranteed-live read have `force_reload_tab`.

## `force_reload_tab` and the in-flight dead-handle race

`SoupCache.force_reload` makes two back-to-back driver calls — `backend.reload(tab_id)`
then `backend.get_page_source(tab_id)` — and the tab can disappear in the gap
between them. When the second call raises `TabNotFoundError`, the post-state
is still clean:

- The `self._entries[tab_id] = …` write is *after* `get_page_source`, so the
  cache is never half-written. Any pre-existing stale entry for that id is
  dropped by `BrowserSessionManager._drop`; no corrupt entry replaces it.
- `BrowserSessionManager.force_reload_tab` catches the exception with the supplied
  `tab_id` (no `current_page_id()` fallback to resolve), drops cache +
  registry for that id, and returns the standard
  `{"error": …, "tab_id": tab_id}` envelope. The agent sees the same dead-tab
  signal as on any other path, recovers via `list_tabs`, and moves on.
- The successful first call (`reload`) is a wasted driver round-trip — Chrome
  handled its own teardown for the now-gone tab; there's nothing to undo. The
  `page_info` it returned is discarded because the contract of
  `force_reload_tab` is "fresh DOM in the cache," and without `get_page_source`
  we can't honor that.

We don't probe existence before reloading (round-trip per call for a race that
almost never fires), don't retry (the tab is gone), and don't try to salvage
the `reload` `page_info` (no DOM to back it). The post-condition — clean cache,
clean registry, structured error — is what we'd want even if `reload` hadn't
happened, which is why the partial failure needs no special handling beyond
the existing dead-handle catch.

## What's `reloaded` for the caller, really

The tool envelope's `reloaded` field is a small contract:

- `false` — "I served you the cached snapshot." Either the first read of a
  freshly-loaded tab, or a hit within TTL. Quick.
- `true` — "Your TTL had elapsed, so I reloaded the tab and re-parsed before
  answering." Slow (full nav + parse), and any client-side state the tab had
  accumulated since the original load is gone.

An agent that doesn't care can ignore it. An agent that's screen-scraping a
tab across many tool calls can watch for an unexpected `reloaded: true` and
re-establish whatever assumptions it was carrying (e.g., scroll position,
expanded sections, form fills).

## Concurrency

Cache reads, writes, and invalidations are plain dict ops, all on the asyncio
loop thread — atomic with respect to other coroutines because asyncio can't
preempt a function mid-execution. The only work that leaves the loop thread
is the backend call inside `get_soup`'s miss / stale branch (and
`force_reload`'s reload), and that goes through `BrowserSessionManager._run_driver`
which sets `_driver_busy` so the reaper can't sweep concurrently. The full
analysis — including the one gap (two tool coroutines reaching `to_thread`
together) and why the serial MCP client closes it — is in
[`cleanup_resources.md`](cleanup_resources.md#concurrency-challenges--resolution).

## Memory

The cache has **no max entry count and no per-entry size cap.** Bounded
implicitly by:

- number of open tabs (one entry per `tab_id`), which the idle reaper keeps
  finite (every tab dies an hour after the last touch);
- dead-handle eviction and reconcile, which drop entries for tabs that no
  longer exist;
- explicit invalidation by `navigate` / `new_blank_tab` / `close_tab` /
  `force_reload_tab` overwriting in place rather than accumulating.

A single tab's BeautifulSoup tree can be a few MB on a heavy SPA, so worst
case a long-lived session with N idle tabs in the last hour holds N × (size
of one parsed DOM) of memory. If that ever matters in practice, the next
knob is a size cap or an LRU eviction at some `MAX_ENTRIES` — both
straightforward to add to the dict-backed `SoupCache`.

## Tunables

In `web_navigator/soup_cache.py`:

- `TTL_SECONDS = 3600` — how long a cached soup is reused before a query
  triggers a reload-and-re-parse. Shorter = fresher views, more reloads.

Related tunables in `web_navigator/session.py` (separate concerns but they
interact with cache lifetime):

- `IDLE_TTL_SECONDS = 3600` — how long a tab can go untouched before the
  reaper closes it (and drops its cache entry).
- `REAP_INTERVAL_SECONDS = 300` — how often the reaper wakes. Bounds the lag
  between "human closed a tab" and "cache entry for that tab gets dropped"
  (if no tool call touches it sooner).

`TTL_SECONDS == IDLE_TTL_SECONDS` by coincidence, not by requirement — the
DOM cache and the idle-tab policy are independent.
