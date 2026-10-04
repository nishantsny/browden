# Gate atomicity

**Status:** implemented · **Applies to:** the write tools (`click`,
`insert_text`, `press_key`), the read tools (`get_element_by_id`,
`get_elements_by_class_name`, `query_selector`, `query_selector_all`,
`screenshot`), `navigate`, `force_reload_tab` and `list_tabs`

## Context

Every tool that touches a page is authorized by a gate:

- **Read gate** (H2): the tab's live URL must pass the read policy (denylist,
  master switch, Tranco, overrides, scheme gate) before its content (HTML,
  pixels) leaves the browser.
- **Write gates:**
  1. **Page:** the denylist veto, then "this action has a rule for this page"
     (`check_action_host`).
  2. **Element and label:** exactly one real, visible, non-decoy control; for
     an anchor, its target must be on the read allowlist; its visible label (or
     key, or field id) must match the page rule (`validate_*_target`).
- **Landing gate:** a navigation or reload can be redirected anywhere (3xx,
  meta or JS redirects). The URL it lands on must pass the read gate, or the
  tab is bounced to `about:blank`.

The browser is shared state. Each session's driver sits behind a FIFO
`asyncio.Lock`, and the config hot-reload runs as a task on the same event loop
as the tools.

## Problem

The tool layer checked and then acted in **separate driver-lock holds**, with
`await`s between them:

```
write:  document_url() | gate 1 | query_selector_all() | gates 2–3 | click()
read:   document_url() | read gate | query_selector()  (cache, or a TTL reload)
nav:    navigate()     | landing gate | navigate("about:blank")
```

Anything that got the lock, or ran on the loop, between those holds changed
what the tool acted on or read, or what it was judged by, after it had been
judged:

- **A concurrent `navigate` on the same tab retargets a write.** The agent sends
  `click`, then `navigate` while `click` is still querying. The lock is FIFO, so
  `navigate` runs before the click's last hold, and the click looks up its
  selector on the new page. That page never passed a write rule or a label
  check. The agent can trigger this reliably, with no timing luck.
- **A concurrent redirecting `navigate` retargets a read.** The agent sends a
  read, then `navigate`s to an allowed URL whose server redirects it off-list.
  The navigate lands off-list before the read's second hold, and its bounce is
  queued *behind* the read. So the read returns the off-list page's HTML or
  screenshot. This needs any redirect from an allowed site, which is common:
  open redirects, login redirects.
- **The soup cache's TTL reload is ungated.** A read whose cached snapshot has
  expired reloads the tab inside the read. If the reload is redirected
  off-list (a session expiry bouncing to SSO on another domain), the read
  returns that page. The gate had checked the URL from before the reload. No
  concurrency is needed.
- **A config hot-reload between two config reads.** A write's gate 1 is judged
  by the old rules and gates 2–3 by the new ones. Gate 3 doesn't repeat the
  denylist check, so the combination can authorize an action that **neither**
  config allows (GHSA-4mgj-cwrw-795x).
- **A stale snapshot.** The write gates judged the soup cache, which can predate
  what the page shows now. The action runs on the live page.

All five come from one cause: **the check and the thing it guards are not one
unit.**

## Decision

The session runs the check and the thing it guards in **one driver-lock hold**.
Wherever a navigation or reload inside that hold lands, the landing is checked
before the hold ends.

```
write   session.click(sel, id=, gate=WriteGate)           one hold, off the loop:
            select_tab · url = document_url · gate.check_page(url)
            found = css_all(parse(get_tab_html()))           fresh parse of the live page
            gate.check_element(url, sel, found) · click_element(sel)

read    session.query_selector(sel, id=, gate=ReadGate)   one hold:
            select_tab · gate.check_page(document_url)
            soup = cache, or a TTL reload → gate the landing BEFORE fetching it; off-list: bounce + refuse
            query soup

nav     session.navigate(url, id=, gate=ReadGate)         one hold:
            navigate(url) · gate the landing → bounce to about:blank if off-list

reload  session.force_reload_tab(id=, gate=ReadGate)      one hold:
            gate.check_page(document_url) · reload · gate the landing → bounce · only then fetch
```

- **`WriteGate`** (`validator/write_gates.py`) and **`ReadGate`**
  (`validator/read_gates.py`) are frozen objects built per request, by
  `click_gate` / `write_text_gate` / `press_key_gate` / `read_gate`, from
  **one** read of the access rules. So a request is judged by a single
  configuration.
- **The session layer stays policy-agnostic.** It runs whatever gate it is
  handed and knows nothing about hosts, labels or the denylist. The access
  decisions still live in `read_gates.py` / `write_gates.py`, as pure
  functions.
- **There is no ungated path.** `gate` is a required argument of every session
  method that reads page content, writes, navigates, reloads or lists tabs. A
  test that wants the raw primitive passes an explicit `OPEN_GATE` /
  `OPEN_READ_GATE`. The session exposes no ungated way to read a tab's URL (it
  once had a public `document_url()`): a URL read in its own hold is stale by
  the time anything acts on it, so the gates read `backend.document_url()`
  inside the hold they guard, and nothing else reads it at all.
- **The server imports no gate predicate.** `server.py` builds gates
  (`read_gate`, `click_gate`, …) and hands them to the session; it never
  calls `ensure_url_allowed` / `check_action_host` / `validate_*_target`
  itself. Checking in the server means checking outside the hold. (It still
  calls `validate_url` on the *requested* URL of a `navigate`, which is a
  string, not a page.)
- **`list_tabs` closes off-list tabs in the listing's own hold (H2).** The
  listing, the read gate on each tab's URL and the close of any off-list tab
  are one hold, so no other request sees an off-list tab in between. A tab
  that can't be closed (the last one) is still never listed.
- **The bounce is part of the hold.** `_bounce_off_list_landing` navigates to
  `about:blank` and drops any snapshot the landing left in the soup cache
  before the hold ends, so no other request ever sees a tab resting off-list.
  The server's old `_guard_landing` is gone.

## Alternatives considered

| Option | Why not |
|---|---|
| **A. Read the config once per request**, keeping the tool-layer sequence | Fixes the hot-reload case only. The concurrent `navigate`, the TTL reload and the stale snapshot remain. It also relies on every future tool following the pattern. |
| **B. Make the last write gate re-run gate 1** (denylist + page rule) | Also fixes only the hot-reload case, and leaves the double read in place looking like the bug. |
| **C. One combined write gate after the query** | Reads the DOM of pages that gate 1 should have refused, and the query's error envelope went back to the agent. |
| **D. A reload counter checked before acting** | Catches reloads only, and adds machinery for the least exploitable of the problems. |
| **E. A read/write lock between reloads and requests** | Brings back the locking the lock-free refresher was built to avoid, and a reload would queue behind requests that can hold the driver for up to 10 s. |
| **F. A tool-layer helper** that sequences the existing calls | Centralizes the code but not the atomicity: the holds are still separate. |
| **G. Re-check the URL after the read**, in a further hold | The content has already been fetched by then, and the second check has its own gap. |

Only moving the check **inside the hold that does the work** closes all five
problems. The config read-once (A) falls out of it for free, because the gate
is built from a single snapshot.

## Tradeoffs taken

- **Policy code now runs inside the session layer's critical section**, and in
  the worker thread. It's safe: the checks are pure functions over an immutable
  rule set, which is thread-safe by construction. The cost is a less strict
  layering, since the session now runs callbacks it doesn't own. We accepted
  that because the lock that makes the read or write safe lives in the session,
  so the decision has to run there too.
- **The driver lock is held longer per request.** A write's hold now includes an
  HTML fetch, a parse and the gates, as well as the action. A read's hold adds
  one `document_url` round trip. Other requests on the same profile wait that
  much longer (the 10 s busy timeout is unchanged). Correctness wins over the
  latency.
- **Writes never use the soup cache.** Every write costs one `page_source` round
  trip and one parse, even when a fresh snapshot is cached. That's what makes
  the write gate judge the live page. The fresh parse isn't stored, because a
  successful write invalidates the tab's cache anyway.
- **A cold read focuses its tab twice** (before the URL check, and inside the
  soup cache), within one hold. That's one extra, cheap `switch_to.window`.
- **A refused request doesn't `touch` the tab's idle registry entry.** Before,
  the separate `document_url` call did. A refused request is not activity
  worth keeping a tab alive for.
- **Tool-level unit tests can no longer check "the session was never called".**
  The session is always called now, and refusals happen inside it. The tool
  tests use fakes (`test/unit/mcp/gated_fakes.py`) that run the real gate and
  record only what was actually performed. The race tests use a real session
  over a fake backend (`test/unit/mcp/atomicity_harness.py`).

## What still isn't atomic

- **Page JS inside the hold.** Between `document_url` and `get_tab_html` (or
  `screenshot`), and between a write's parse and the backend's live
  `_resolve_one_visible`, the page's own scripts can still navigate or change
  the DOM. The windows are a few WebDriver round trips, and no other *request*
  can reach them, but the page itself can. For writes, the backend refuses an
  ambiguous or missing live match, but it does not re-judge the label. Closing
  this fully means gating what was actually fetched: the resolved element's
  `outerHTML`, or a `document.URL` read in the same script as the content.
- **A cached snapshot from an earlier page.** The soup cache is keyed by tab,
  not by URL, and is only invalidated by browden's own navigations and writes.
  If the page's JS navigates the tab, a read gates the new live URL but can be
  answered from a fresh-by-TTL snapshot of the previous page. That page was
  admitted when it was fetched, but possibly under rules that have since been
  tightened. Keying cache entries by URL would close this.
- **The action's effect.** A click that navigates, or that runs JS, can do
  anything the page does once clicked. For anchors, gate 2b bounds where an
  `href` can navigate, but the gates judge the control, not its consequences.
- **A reload during the hold** takes effect on the next request. The request in
  flight finishes under the rules it was judged by, which is the refresher's
  stated contract.

## Invariants for new tools

1. Build the gate (`read_gate`, or a `WriteGate` factory) from **one**
   `_access_rules_for(session)` call.
2. Hand it to a session method that runs it **and** the read or action in one
   `_with_tab` hold: through `_gated_soup` / `_check_live_url` for reads, and
   `_gated_write` for writes.
3. Anything in that hold that navigates or reloads gates its landing with
   `_bounce_off_list_landing` before the hold ends.
4. Writes judge a fresh parse of the live page, never the soup cache.
5. Classify the tool in `TOOLS` in `test/unit/mcp/test_gate_races.py` (as
   `READ`, `WRITE` or `LANDING`, with the backend calls it makes as its
   `park_points`). That one entry runs it through every race of its kind: paused
   at each of those calls while a `navigate` to a readable-but-unwritable page,
   and to an allowed URL that redirects off-list, queues behind it. The
   invariant checked after every race is the same: no off-list content left the
   browser, and no write landed on a page without a write rule.
6. A reload in a hold — `force_reload_tab`, or the soup cache's TTL reload —
   gates its landing through the cache's `on_reload` hook, which runs *before*
   the landed page is fetched. Gating after the fetch keeps the content from the
   agent, but it has still left the browser.
