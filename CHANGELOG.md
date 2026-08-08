# Changelog

All notable changes to browden are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Only the latest release is supported
(see [SECURITY.md](./SECURITY.md)).

## [Unreleased]

### Added
- **`invalidate_dom_cache` tool.** Drops a tab's cached DOM snapshot so the next
  read re-fetches the live HTML, without reloading the page — the fix for a
  change the *page itself* made after the snapshot was taken (its own JS
  revealing a panel, an infinite-scroll batch landing). Unlike `force_reload_tab`
  it issues no page load, so JS-built DOM state survives. It drives no browser
  action and returns no page content, so it isn't read-gated; reads stay gated on
  the tab's live URL at read time. A tab that is gone returns the usual tab-gone
  envelope (#137).

## [1.2.1] — 2026-07-29

### Fixed
- **Concurrent requests to one profile no longer read each other's tabs.** A
  session's tabs share one focused window and one (not thread-safe) WebDriver,
  but the MCP server dispatches every incoming request concurrently — so two
  in-flight tools would interleave their `select_tab` + read, and a tab came
  back with whichever document won the last focus change. Each
  `BrowserSessionManager` now holds an `asyncio.Lock` that every driver touch
  goes through, making "focus the tab, then act on it" atomic: concurrent
  requests queue and each re-selects its own tab when its turn comes. A new e2e
  test fires ten simultaneous reads at ten tabs in one session and asserts each
  gets its own page — it failed on 6/10 tabs before this change.

### Changed
- **Concurrency contract, restated.** Concurrent requests within a session are
  now *safe* (serialized) rather than unsupported; they are still not *parallel*
  — use separate `profile_dir`s for that. The MCP instructions, the
  `new_blank_tab` / `list_tabs` descriptions and the README's Profiles section
  say so.
- **A request that waits more than 10s for its session gives up.** Rather than
  queueing forever behind a wedged operation, it returns
  `{"error": "browser session busy — ...", "id": ...}` (`SessionBusyError`) and
  can be retried. `list_tabs` reports a busy profile in place of that profile's
  tabs instead of failing the whole listing or silently showing none.
- **Cleanup takes the same lock.** Both sweep callers — the reaper's tick and
  `new_blank_tab`'s at-the-cap reclaim — drive the browser (`list_handles` /
  `close_tab`), so both now run through `_sweep_idle_locked`. Best-effort: a
  sweep that can't get the lock in time is skipped and the next tick catches up.
  This replaces the old `_driver_busy` flag, which the lock subsumes.
- **Idle cleanup is no longer done on the request path.** Every tool call used
  to run the idle sweep first — a live `list_handles()` round-trip to the browser
  on *every* call, to maybe close a tab that had been untouched for an hour. The
  sweep now runs only on the reaper's timer, so a tool call costs exactly the
  driver work it asked for.
- **The reaper's cadence moved to the config: `infra.reap_interval_seconds`,
  defaulting to 7200 (2h).** Like every other allowlist setting it is
  live-reloaded: the reaper re-reads it each tick, so an edit lands on the
  next wake-up without restarting the server. A tab is now closed between 1h
  (`IDLE_TTL_SECONDS`) and 1h + one interval after its last *agent* use; lower
  the interval to reclaim tabs sooner.
- **Hitting the per-session tab cap now reclaims idle tabs and retries.** With
  tool calls no longer sweeping, a session whose tabs had gone idle would sit
  wedged at `max_tabs_per_session` until the reaper's next tick; instead
  `new_blank_tab` sweeps once when it finds the session full and tries again.
  It is the only sweep outside the reaper's tick, so the cost falls on the rare
  request that would otherwise fail rather than on every call — and
  `session limit of N tabs reached` now means there was genuinely nothing idle
  left to reclaim.
- One consequence of dropping the lazy sweep remains, bounded by the interval:
  tabs the human closed in Chrome stay tracked until the next reconcile. Acting
  on one already returned the tab-gone envelope, so this is a bookkeeping delay,
  not a behaviour change for the agent.

## [1.2.0] — 2026-07-28

### Added
- **`press-key` write action.** A new tool/section that focuses an element and
  sends a single **control key** (Enter/Space/Tab/Escape/arrows/Home/End/
  Page{Up,Down}) — keyboard activation for controls a coordinate `click` can't
  reach, e.g. `tabindex` list rows or ARIA widgets that aren't `<button>`/`<a>`.
  Default-deny, gated in parallel to `click`: the element must be a real, visible,
  non-decoy **focusable** control (`is_focusable_control` — natively focusable or
  carrying `tabindex`; a bare `<div onclick>` is refused), the key must be a
  control key (never a character — typing stays `write-text`'s job), and some
  page rule matching the URL must admit the control by `label` **and** list the
  key in a new per-rule `keys:` set. Not hit-tested, so it also isn't blocked by
  overlays. Sample: `configs/samples/allow_press_key_activation.yaml` (#126).

## [1.1.0] — 2026-07-22

### Added
- **Page-scoped allowlist rules.** A `click` / `write-text` host, or a
  `website_overrides` entry, may now map to an ordered list of page rules —
  each scoping its `label` / `field_ids` (or read access) to only the pages its
  `path` selector matches, with `match_on: path` (default) or `match_on: url`
  (path + query + fragment, for hash-routed SPAs). Authority is per page rather
  than per registrable domain, and every pre-existing config still loads (#123).

### Security
- The read/write tools now gate on the **focused document's URL** (`document.URL`,
  via the new `backend.document_url()`) instead of the top-level `current_url`.
  These are identical today — nothing shifts the driver off the top document — so
  this is behavior-preserving; the point is that every read/write validation is now
  keyed on the document actually being acted on, so once frame focus exists the gates
  validate the frame's own URL rather than the top page's. Navigations still validate
  the target URL, and `list_tabs` still gates each tab's top URL.

## [1.0.0] — 2026-07-18

First stable release: hardens the safety perimeter across the board and pins the
install to a reproducible, locked dependency set.

### Security
- Gate DOM reads, screenshots, and reloads on the tab's **live** URL, and close
  (not just hide) off-allowlist tabs in `list_tabs` (H2, #84).
- Match Tranco membership on the host's **registrable domain** via the Public
  Suffix List, so shared-hosting subdomains (`evil.github.io`) never inherit
  the provider's rank; one shared host canonicalizer for every gate — trailing
  dots and `www.` can no longer make two gates disagree (H1/H3, #85, #81).
- **https-only scheme gate**: `file://` / `ftp://` / plaintext `http://` are
  refused unless the host is explicitly opted in via `website_overrides`
  (M2, #76).
- Re-validate the **post-redirect landing URL** on navigate/reload; an
  off-allowlist landing bounces the tab to `about:blank` (M3, #74).
- Normalize URL paths (`%2e`, `.` / `..` segments) before allowlist matching,
  the way Chrome resolves them (M1, #75); allow-list path regexes must match
  the whole path (#79).
- Pin the Chrome DevTools websocket origin instead of
  `--remote-allow-origins=*` (M4, #73); make `--no-sandbox` opt-in only
  (`BROWDEN_NO_SANDBOX`, #80).
- XML-escape values rendered into the Windows task definition (#83).

### Added
- Hot-reload the allowlist config every ~10s while the server runs; a bad edit
  keeps the last-good policy (#88).
- Fetch the Tranco snapshot by resolving the current daily list id, under a
  bounded read cap (#94); default read-allowlist `top_n` raised to 1M (#77).
- Setup records perimeter provenance in `allowlist.yaml`: the Tranco list id
  and sha256 checksums of the fetched Tranco/PSL snapshots, written as a
  greppable comment block, so an install is auditable and reproducible (#112).

### Changed
- Uniform tab-targeting in the backend (callers always focus first) and a
  single per-tab op skeleton in the session manager (#93, #89, #91).
- Reproducible installs: commit `uv.lock` and adopt the `uv sync` flow — the
  one-time installer and CI install the exact locked set with `--frozen`, while
  local development stays free to re-resolve (#107).
- Memoize `PopularityAllowlist.contains` per instance so repeated allowlist
  lookups skip the redundant registrable-domain work (#110).

### Fixed
- `select_tab` serializes `selected` as a real boolean and returns the tab-gone
  envelope when the target tab has disappeared (#108).
- Logging resolves `stderr` at emit time and no longer emits atexit teardown
  noise after unit runs (#109).

## [0.1.0] — 2026-07-07

Initial public release.

- MCP server over a real, **undetected** Chrome: browden launches Chrome
  itself (no chromedriver automation flags) and attaches Selenium over the
  DevTools port.
- Read-first tool surface: tab management (`list_tabs`, `new_blank_tab`,
  `select_tab`, `close_tab`, `navigate`, `force_reload_tab`), server-side DOM
  queries (`get_element_by_id`, `get_elements_by_class_name`,
  `query_selector`, `query_selector_all`), and `screenshot`.
- Three-layer YAML policy: an always-wins **denylist**, a **read allowlist**
  (Tranco top-sites plus per-host path-regex overrides), and **default-deny
  write actions** (`click`, `insert_text`), each gated per host by a required
  visible-`label` regex — with anti-decoy integrity checks against
  agent-targeted page bait.
- One profile = one Chrome: per-profile sessions run concurrently; per-tab DOM
  cache with idle-tab reaping; resource caps (`infra.max_browser_sessions`,
  `infra.max_tabs_per_session`).
- One-time setup for Linux/macOS/Windows: venv install, sample allowlist,
  Tranco snapshot fetch, and an optional background SSE service via the native
  service manager (systemd / launchd / Task Scheduler).
- Apache-2.0.

[Unreleased]: https://github.com/nishantsny/browden/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/nishantsny/browden/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/nishantsny/browden/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/nishantsny/browden/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/nishantsny/browden/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/nishantsny/browden/releases/tag/v0.1.0
