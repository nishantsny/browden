# Write-gate atomicity

**Status:** implemented · **Applies to:** `click`, `insert_text`, `press_key`

## Context

A write action is authorized by a sequence of gates:

1. **Page:** the denylist veto, then "this action has a rule for this page"
   (`check_action_host`).
2. **Element and label:** exactly one real, visible, non-decoy control; for an
   anchor, its target must be on the read allowlist; its visible label (or key,
   or field id) must match the page rule (`validate_*_target`).

Then the action runs. The browser is shared state. Each session's driver sits
behind a FIFO `asyncio.Lock`, and the config hot-reload runs as a task on the
same event loop as the tools.

## Problem

The tool layer ran the sequence as **three separate driver-lock holds**, with
`await`s between them:

```
document_url()          lock · read URL · unlock
gate 1                  reads the live config
query_selector_all()    lock · query the cached DOM snapshot · unlock
gates 2–3               reads the live config again
session.click()         lock · look up the selector on the LIVE page · click · unlock
```

Anything that got the lock, or ran on the loop, between those holds changed
what the action acted on, or what it was judged by, after it had been judged:

- **Concurrent `navigate` on the same tab.** The agent sends `click`, then
  `navigate` while `click` is still querying. The lock is FIFO, so `navigate`
  runs before the click's last hold, and the click looks up its selector on
  the new page. That page never passed a click rule or a label check. The agent
  can trigger this reliably, with no timing luck.
- **Config hot-reload between the two config reads.** Gate 1 is judged by the
  old rules and gates 2–3 by the new ones. Gate 3 doesn't repeat the denylist
  check, so the combination can authorize an action that **neither** config
  allows (GHSA-4mgj-cwrw-795x).
- **Stale snapshot.** Gates 2–3 judged the soup cache, which can predate what
  the page shows now (the page's own JS relabelled a button). The action runs
  on the live page.

All three come from one cause: **the decision and the action are not one
unit.**

## Decision

The session runs the whole sequence and the action in **one driver-lock hold**:

```
session.click(css_selector, id=, gate=)       one lock hold, off the loop:
    select_tab
    url   = document_url()
    gate.check_page(url)                       gate 1, before any DOM is read
    found = css_all(parse(get_tab_html()))     a fresh parse of the live page
    gate.check_element(url, css_selector, found)   gates 2+
    click_element(css_selector)
```

- **`WriteGate`** (`validator/write_gates.py`) is a frozen pair of checks. It is
  built per request by `click_gate` / `write_text_gate` / `press_key_gate` from
  **one** read of the access rules, so a request is judged by a single
  configuration.
- **The session layer stays policy-agnostic.** It runs whatever gate it is
  handed and knows nothing about hosts, labels or the denylist. The access
  decisions still live in `write_gates.py`, as pure functions.
- **There is no ungated write path.** `gate` is a required argument of every
  session write method. A test that wants the raw primitive passes an explicit
  `OPEN_GATE` (`test/e2e/gates.py`).

## Alternatives considered

| Option | Why not |
|---|---|
| **A. Read the config once per request**, keeping the tool-layer sequence | Fixes the hot-reload case only. The concurrent `navigate` and the stale snapshot remain. It also relies on every future tool following the pattern. |
| **B. Make the last gate re-run gate 1** (denylist + page rule) | Also fixes only the hot-reload case, and leaves the double read in place looking like the bug. |
| **C. One combined gate after the query** | Reads the DOM of pages that gate 1 should have refused, and the query's error envelope went back to the agent. |
| **D. A reload counter checked before acting** | Catches reloads only, and adds machinery for the least exploitable of the three problems. |
| **E. A read/write lock between reloads and requests** | Brings back the locking the lock-free refresher was built to avoid, and a reload would queue behind requests that can hold the driver for up to 10 s. |
| **F. A tool-layer helper** that sequences the existing calls | Centralizes the code but not the atomicity: the holds are still separate. |

Only moving the sequence **inside one hold** closes all three problems. The
config read-once (A) falls out of it for free, because the gate is built from
a single snapshot.

## Tradeoffs taken

- **Policy code now runs inside the session layer's critical section**, and in
  the worker thread. It's safe: the checks are pure functions over an immutable
  rule set, which is thread-safe by construction. The cost is a less strict
  layering, since the session now runs callbacks it doesn't own. We accepted
  that because the lock that makes the action safe lives in the session, so
  the decision has to run there too.
- **The driver lock is held longer per write.** The hold now includes an HTML
  fetch, a parse and the gates, as well as the action. Other requests on the
  same profile wait that much longer (the 10 s busy timeout is unchanged).
  Writes are rare and interactive, so latency matters less than correctness.
- **Writes never use the soup cache.** Every write costs one `page_source` round
  trip and one parse, even when a fresh snapshot is cached. That's what makes
  the gate judge the live page. The fresh parse isn't stored, because a
  successful write invalidates the tab's cache anyway.
- **A refused write doesn't `touch` the tab's idle registry entry.** Before,
  the separate `document_url` call did. A refused write is not activity worth
  keeping a tab alive for.
- **Tool-level unit tests can no longer check "the session was never called".**
  The session is always called now, and refusals happen inside it. The tool
  tests use a fake (`test/unit/mcp/gated_write_fake.py`) that runs the real
  gate and records only what was actually performed.

## What still isn't atomic

- **Page JS between the parse and the action.** Inside the hold, the page's own
  scripts can still change the DOM between `get_tab_html` and the backend's
  live `_resolve_one_visible`. The backend refuses an ambiguous or missing live
  match, but it does not re-judge the label. Closing this fully means gating
  the resolved live element (its `outerHTML`) rather than a parse of the page.
- **The action's effect.** A click that navigates, or that runs JS, can do
  anything the page does once clicked. For anchors, gate 2b bounds where an
  `href` can navigate, but the gates judge the control, not its consequences.
- **A reload during the hold** takes effect on the next request. The write in
  flight finishes under the rules it was judged by, which is the refresher's
  stated contract.

## Invariants for new write tools

1. Build a `WriteGate` from **one** `_access_rules_for(session)` call.
2. Hand it to a session method that runs it **and** the action in one
   `_with_tab` hold, through `_gated_write`.
3. Judge a fresh parse of the live page, never the soup cache.
4. Add the tool to `WRITES` in `test/unit/mcp/test_write_gate_atomicity.py`, so
   the concurrent-`navigate` race test covers it.
