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
before the hold ends. Inside the hold, every read of page content and every
action goes through a **`GatedPage`** (`session_management/gated_page.py`),
which gates what it actually fetched or acted on — not just the URL it saw
first.

```
write   session.click(sel, id=, gate=WriteGate)           one hold, off the loop:
            select_tab · gate.check_page(document_url)
            snap = target_snapshot(sel)                      ONE script: document.URL, outerHTML,
                                                             live match count, tag, element ref
            snap.url moved? gate.check_page(snap.url)
            gate.check_element(snap.url, sel, css_all(parse(snap.html)))
            live count == 1 and same tag as the parse · click_target(snap.ref)

read    session.query_selector(sel, id=, gate=ReadGate)   one hold:
            select_tab · url = document_url · gate.check_page(url)
            cache entry for (tab, url)?  fresh → use it
                                         stale → reload · gate the landing BEFORE fetching; off-list: bounce + refuse
            else snap = page_snapshot()                      ONE script: document.URL + outerHTML
                 gate snap.url (off-list: bounce + refuse) · cache under snap.url
            query soup

shot    session.screenshot(id=, gate=ReadGate)             one hold:
            gate.check_page(document_url) · screenshot · URL moved? gate it again

nav     session.navigate(url, id=, gate=ReadGate)         one hold:
            navigate(url) · gate the landing → bounce to about:blank if off-list

reload  session.force_reload_tab(id=, gate=ReadGate)      one hold:
            gate.check_page(document_url) · reload · gate the landing → bounce · only then snapshot
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
  `OPEN_READ_GATE`.
- **Only `GatedPage` touches page content.** Session methods get a `GatedPage`
  (via `_with_page`), never the backend, for anything that reads or acts on a
  page. `test/unit/mcp/test_gated_page_guard.py` parses every module and fails
  if anything else calls a backend content method (`page_snapshot`,
  `target_snapshot`, `document_url`, `screenshot`, `navigate`, `reload`, the
  `*_target` actions) or fills the soup cache. Every backend method must be
  classified there as lifecycle or content, so a new one is caught too. This is
  what keeps a check-then-read across two holds from coming back. The session exposes no ungated way to read a tab's URL (it
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
- **The bounce is part of the hold.** `GatedPage._bounce` navigates to
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
- **The driver lock is held longer per request.** A write's hold now includes a
  snapshot script, a parse and the gates, as well as the action. A read's hold adds
  one `document_url` round trip. Other requests on the same profile wait that
  much longer (the 10 s busy timeout is unchanged). Correctness wins over the
  latency.
- **Writes never use the soup cache.** Every write costs one snapshot round
  trip and one parse, even when a fresh snapshot is cached. That's what makes
  the write gate judge the live page. The fresh parse isn't stored, because a
  successful write invalidates the tab's cache anyway.
- **A read checks the URL twice on a fetch**: `document_url` before it, so an
  off-list page is never fetched in the common case, and the snapshot's own
  URL after it, so what was fetched is what was judged. The first costs one
  round trip.
- **A write needs the parse and the browser to agree.** The gates judge the
  element as `html.parser` + soupsieve see it; the action uses the element the
  browser's `querySelectorAll` returned in the same script. If the two disagree
  on the count or the tag, the write is refused rather than guessed at.
- **The cache holds one page per tab, keyed by URL.** A tab whose page's JS
  moved it to another URL misses and refetches, even if it moves back.
- **A refused request doesn't `touch` the tab's idle registry entry.** Before,
  the separate `document_url` call did. A refused request is not activity
  worth keeping a tab alive for.
- **Tool-level unit tests can no longer check "the session was never called".**
  The session is always called now, and refusals happen inside it. The tool
  tests use fakes (`test/unit/mcp/gated_fakes.py`) that run the real gate and
  record only what was actually performed. The race tests use a real session
  over a fake backend (`test/unit/mcp/atomicity_harness.py`).

## What still isn't atomic

- **Between the write's snapshot and its action.** The action runs on the
  element the snapshot judged (by reference, not by re-finding the selector),
  and an element the page has since replaced is refused as stale. But the page
  can still change that same element's text or attributes in the one round trip
  before the action, and it is not re-judged.
- **Between a screenshot's checks.** The URL is gated before and after the
  capture; a page that navigated away and back between them isn't seen.
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
   hold, through `_with_page`: the work gets a `GatedPage` and calls its
   gated methods (`soup`, `screenshot`, `reload`, `navigate`, `click`, …).
3. A new kind of read or action is a new `GatedPage` method, and a new backend
   primitive is classified in `test_gated_page_guard.py`. It gates the URL the
   content or element actually came from — read in the same script as the
   content, as `page_snapshot` / `target_snapshot` do — and any navigation or
   reload in it gates its landing with `_bounce` before anything is fetched.
4. Writes judge a fresh snapshot of the live page, never the soup cache, and act
   on the element that snapshot returned.
5. Classify the tool in `TOOLS` in `test/unit/mcp/test_gate_races.py` (as
   `READ`, `WRITE` or `LANDING`, with the backend calls it makes as its
   `park_points`). That one entry runs it through every race of its kind: paused
   at each of those calls while a `navigate` to a readable-but-unwritable page,
   and to an allowed URL that redirects off-list, queues behind it. The
   invariant checked after every race is the same: no off-list content left the
   browser, and no write landed on a page without a write rule.
