# Changelog

All notable changes to browden are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Only the latest release is supported
(see [SECURITY.md](./SECURITY.md)).

## [Unreleased]

### Changed
- **README: the intro now leads with the gating thesis rather than "read-only".**
  The opening pitched the project as a read-only shell that happens to be
  configurable, which undersold both halves of it: that the gates are
  deliberately *unintelligent* — host, path and visible-label regexes, with no
  model in them to persuade — and that they are extensive enough for an agent to
  roam and act. The new intro states that, tabulates the five gates and their
  defaults, and documents the scratch-profile posture (`allow_all` in its own
  Chrome profile), including what `allow_all` still does *not* relax: the
  denylist, the scheme gate, `allowed_upload_locations`, and the Tranco read
  check, which stays on unless the profile opts out explicitly. Docs only — no
  behaviour change.
- **A merge during the release e2e no longer blocks the tag (maintainers).**
  `deploy.sh --release` refused to tag unless the deployed commit was still
  `main`'s tip, so any PR merged during the ~7-minute e2e aborted a green run.
  It now tags the commit it deployed and tested, provided that commit is on
  `main` — the same ancestry check the `release` workflow makes.
- **Release skill: a merged prep PR is the go-ahead (maintainers).** After the
  `release: cut vX.Y.Z` PR merges, `maintainers/github-release/SKILL.md` now
  goes straight to the `--release` deploy instead of asking again whether to
  run it; it still asks when the version was already on `main`.

## [1.5.0] — 2026-10-04

### Added
- **The DOM-read caps are now visible from the tool schema, and documented.** The
  four read tools truncate `text` at 2000 chars unconditionally, cap attribute
  values at 256, and hide outer HTML behind `include_html` — but none of that
  reached an MCP client, which saw four bare typed params. A caller whose payload
  was one large node (a JSON blob rendered in `<pre>`) would reasonably pass a big
  `max_html_bytes`, get 2000 chars of `text` back, and conclude the tool clamps
  output; `max_html_bytes` in fact caps `html` alone and is inert unless
  `include_html=True`. `include_html`, `max_html_bytes`, `limit` and `offset` now
  carry `description`s in the JSON schema, each tool's docstring states the caps,
  and the new [docs/dom-reads.md](./docs/dom-reads.md) documents every cap and
  every field of a returned node — with a README recipe for reading a large
  payload out of a page. No behaviour change: the caps themselves are
  unchanged (#148).
- **`click` can activate a `<label>` bound to a radio or checkbox.** Pages
  routinely hide the native `<input type=radio|checkbox>` in CSS
  (`position:absolute;left:-9999px`, or `display:none`) and draw the visible
  control as a `::before` on its `<label>`. Every route was refused: the input is
  the wrong tag for `click` and, not being rendered, `press-key` rejected it as
  invisible — and the label was neither a clickable tag nor focusable. Such forms
  were readable but undriveable. A `<label>` is now a clickable control when it
  resolves (by `for=` idref, or by wrapping) to a real, enabled, non-decoy radio
  or checkbox; gate 3 matches the **label's own visible text**, which is the
  string a human reads next to the control and a better authorization surface
  than the hidden input's `id`. Only radio and checkbox qualify: a label bound to
  a text field merely moves focus, and one bound to a submit button would let a
  label's text stand in for a button's own gated text. The relaxation that lets a
  hidden target through applies *only* to the control — the label itself is still
  held to full visibility, so nothing invisible to a human is clickable, and
  decoy/`disabled`/`type=hidden` targets are refused either way. `press-key` is
  unchanged. Worked sample:
  [`configs/samples/allow_label_activation.yaml`](./configs/samples/allow_label_activation.yaml) (#146).
- **`upload-file` write action: attach a local file to an `<input type=file>`.**
  New `upload_file` tool, for the one control no existing action could drive:
  `write-text` excludes `type=file` by design, and clicking a file input opens the
  operating system's own file dialog, which is not part of the page and which
  browden cannot drive. The motivating case is attaching a receipt image to a
  Splitwise expense. It is a section of its own, **never** a widening of
  `write-text`'s accepted input types: reading a file off your disk and handing it
  to a website is a different capability from typing into a box, so a host already
  trusted to receive typed text (`label: '.*'` is common) gains nothing from this.
  Four default-deny gates: host+page, a real visible non-decoy non-readonly file
  input, the control authorized by a matching rule's `label` **or** `field_ids`
  (the id half carries this one — a file input routinely has no visible label at
  all, as Splitwise's does not), and a new filesystem gate. `field_ids` is
  therefore now accepted under `upload-file` as well as `write-text`. The path is
  resolved **once**, by the gate, and that exact path is what the browser is
  handed — never a second resolve of the caller's spelling, which could land
  somewhere the gate never saw if a symlink inside an allowed location were
  replaced in between. A path containing a control character is refused (the
  driver splits a path on newlines, so one would name a second file), and a
  `multiple` input is refused for now, so one admitted path is one file at the
  browser.
- **`allowed_upload_locations`: a new top-level allowlist section bounding what may leave the
  machine.** Required for `upload-file` — with none configured nothing is
  uploadable, **including under `allow_all`**, which grants authority over pages
  and says nothing about the filesystem. Deliberately not per-host: it bounds
  which files may be sent anywhere at all, independently of where they are going.
  Without it, "upload to host X" would mean "exfiltrate `~/.ssh/id_rsa` to host
  X". A path is expanded (`~`) and **fully resolved** — through `..` segments and
  every symlink — before it is compared, so neither a traversal
  in the path the agent passes nor a symlink planted inside one reaches outside it; the file must also exist, be a regular file, and be under a 25 MiB cap. A
  profile's locations are additive over the global ones, like every other rule. Worked
  sample: [`configs/samples/allow_receipt_upload.yaml`](./configs/samples/allow_receipt_upload.yaml) (#147).
- **iframe inspection (#117).** New `switch_to_frame`, `switch_to_parent_frame`
  and `switch_to_default_content` tools focus a tab on an `<iframe>`, so the
  existing DOM-read tools (`query_selector`, `get_element_by_id`, `screenshot`,
  …) can inspect its contents, which were previously invisible (the tools only
  ever saw the top document). The frame focus is replayed across the
  window-refocus that nearly every op performs, and reset on `navigate` /
  reload (#118).

### Security
- **A reload redirected off-list no longer fetches the page it landed on.**
  `force_reload_tab`, and a read whose cached snapshot had expired, reloaded the
  tab and fetched its HTML *before* checking where the reload landed. The agent
  never received that page (the landing was bounced and the request refused),
  but its content still left the browser into browden's memory. The landing is
  now checked between the reload and the fetch.
- **Reads and writes gate what was actually fetched, not just the URL seen first.**
  Inside one driver hold, the page's own JS could still navigate between the
  URL check and the fetch, and a write's gates judged a parse of the page while
  the backend then re-found the selector live. Now a read's HTML and its
  `document.URL` come from one script and that URL is gated; a write's page,
  URL, live match count and element come from one script, the gates judge that
  snapshot, and the action runs on that exact element (a replaced element is
  refused as stale). A screenshot re-checks the URL after the capture. The soup
  cache is keyed by URL, so a tab whose page navigated itself is never answered
  from a snapshot of the page it left.
- **`switch_to_frame` is same-origin only and gates the frame as its own
  document, in the one driver hold that switches.** The focused page must be
  read-allowed, the iframe's declared `src` is checked *before* switching, and
  the frame's actual `document.URL` must be read-allowed **and** same-origin
  with the top page *after* switching. All of it runs in the same hold as the
  switch, so nothing (a concurrent `navigate`, a config hot-reload) can land
  between a check and the move. The frame is recorded only once admitted; on any
  failure, including an error reading the landed URL, the focus is put back
  where it was. Cross-origin frames stay refused in this release: reads and
  writes inside a frame are judged by the frame's own URL, but allowing other
  origins is left to a later change. `switch_to_parent_frame` /
  `switch_to_default_content` **re-verify the landed document on every call**,
  not just on entry: another process may have navigated an ancestor (or the top
  page) to an untrusted URL while we were deeper in the tree. On refusal the
  focus retreats to the top document and the call raises. Inside a frame the
  gates never judge the top page in its place: if the frame's URL can't be
  read, the frame has been removed from the page, or a stale-snapshot reload
  returned the tab to its top document, the call is refused with a
  `FrameFocusError` (once; the tab is then at its top document) and the agent
  re-enters the frame. Same-origin is the exact origin (scheme, host and port), and it holds on
  every call, not only on entry: each call re-enters the frame only while the
  top page is unchanged and the frame still holds a document of its admitted
  origin, otherwise the frame is lost. A `screenshot` inside a frame also gates
  the top page's URL, since the capture shows the whole viewport. Writes inside
  a frame are allowed, each judged by the frame's own URL under its own rules
  (a `srcdoc` frame has its parent page's URL, so the parent's rules apply). Returning to the top page (`switch_to_default_content`, or an ascent that
  reaches it) needs only the read check, so a tab at `about:blank` or an
  override-allowed `file://` page can always be returned to, and
  `force_reload_tab` recovers from a lost frame in one call. A worked sample,
  [`configs/samples/allow_iframe_access.yaml`](./configs/samples/allow_iframe_access.yaml),
  shows which frames a portal page can enter and where writes inside them apply.
  Every frame tool judges against the tab's own profile's rules. A frame the
  page wrote itself (`srcdoc`, or an `about:blank` frame filled in by script)
  has no URL of its own and is judged by the URL of the same-origin page that
  wrote it; one the browser keeps from reading that page (a sandboxed frame's
  opaque origin) keeps its `about:` URL and is refused (#118).

### Changed
- **Local test runs keep temp dirs only for failed tests (maintainers).**
  `tmp_path_retention_policy = "failed"` in `pyproject.toml`. Each e2e test's
  `tmp_path` holds a whole Chrome profile, and pytest's default kept every
  test's for the last three runs: about 1.5-2 GB per full e2e run left in
  `/tmp`. A failing test's dir is still kept for debugging.
- **No test stubs a private method (tests).** 19 stubs in 13 tests replaced a
  private helper: the backend's `_launch_chrome`, `_wait_for_devtools`,
  `_free_port`, `_chrome_args`, `_clear_stale_singletons`, `_terminate` and
  `_wellknown_chrome_paths`, and the session's `_run_driver`. A test that
  stubs a helper keeps passing after that helper is renamed or deleted (the
  patch just recreates it), which is how a removed helper reached CI on #118.
  Each test now fakes only the external boundary underneath:
  `subprocess.Popen`, the DevTools `urlopen` probe, the port, the clock, the
  filesystem, `asyncio.to_thread`, or a constructor argument. The private code
  runs for real. No test was removed.
- **`GatedPage` is the only code that reads page content or acts on a page.**
  Session methods get a `GatedPage` (`session_management/gated_page.py`) for
  every read, write, navigation and reload, and never call the backend's content
  methods themselves. `test/unit/mcp/test_gated_page_guard.py` fails the build
  if any other module does, and every backend method must be classified there as
  lifecycle or content. The soup cache is now a plain store; fetching and
  reloading moved into `GatedPage`. **Backend interface:** `get_tab_html` is
  replaced by `page_snapshot`, `target_snapshot` is new, and
  `click_element` / `insert_text_element` / `press_key_element` are replaced by
  `click_target` / `insert_text_target` / `press_key_target`, which act on the
  element a snapshot returned.
- **One race table for every page-touching tool (tests).**
  `test/unit/mcp/test_gate_races.py` classifies each tool that reads, writes,
  navigates or reloads in a single `TOOLS` table (`READ` / `WRITE` /
  `LANDING`). Each tool runs through every race of its kind: paused at each
  backend call it makes while a concurrent `navigate` to a readable-but-unwritable
  page, or to a URL redirected off-list, queues behind it. After every race
  one invariant is checked: no off-list content left the browser, and no write
  landed on a page without a write rule. This replaces the separate read and
  write race lists. A new tool is covered by adding one entry.
- **`list_tabs` gates and closes off-list tabs in the listing's own hold.**
  The server used to list a profile's tabs, check each URL against the read
  gate, and then close the off-list ones in separate driver holds. The session
  now does all three in one hold, through the `ReadGate` the tool hands it. So
  `server.py` no longer calls any gate predicate itself, and no other request
  can run between the check and the close. The listing behaves the same: an
  off-list tab is closed (best-effort; the last tab can't be) and never listed.
- **The ungated `BrowserSessionManager.document_url()` is removed
  (internal).** It read a tab's URL in a hold of its own, so a check built on it
  was stale by the time anything acted. That's the separate-hold pattern
  GHSA-4mgj-cwrw-795x fixed. The gates already read `backend.document_url()`
  inside the hold they guard, and nothing in browden used the session method.

## [1.4.0] — 2026-10-04

### Changed
- **GitHub Release pages lead with the CHANGELOG section (maintainers).** The
  `release` workflow used `--generate-notes` alone, so a Release page listed
  only the titles of the PRs merged since the previous tag, and none of the
  changelog's explanation or compatibility notes. It now puts the tag's
  `## [X.Y.Z]` section first and keeps GitHub's PR list below it. A tag whose
  version has no section (or an empty one) fails the workflow before `stable`
  is promoted.

### Security
- **Write actions are decided and performed as one unit
  (GHSA-4mgj-cwrw-795x).** `click`, `insert_text` and `press_key` used to gate
  the page, query the element, judge it and act in three separate driver-lock
  holds, reading the live config twice. So a concurrent `navigate` on the same
  tab could land the action on a page no write rule authorizes, and a config
  hot-reload between the two reads could authorize an action neither config
  allows. The session now runs every gate and the action in one hold, against
  one read of the rules and a fresh parse of the live page rather than the DOM
  cache.
- **Reads are gated on the page they actually read.** The DOM reads and
  `screenshot` checked the tab's URL in one driver-lock hold and read it in
  another. So a concurrent `navigate` to an allowed URL that redirects off-list
  could hand the read the off-list page. Separately, a read whose cached
  snapshot had expired reloaded the tab without re-checking where the reload
  landed. The session now checks the live URL in the same hold as the read, and
  gates the landing of every navigation and reload (including the cache's TTL
  reload) before that hold ends. An off-list landing is bounced to
  `about:blank` there, so no other request sees the tab off-list.
- Design and tradeoffs for both: `docs/design/gate-atomicity.md`.

## [1.3.1] — 2026-10-04

### Changed
- **Deploy script syncs the service's venv from `uv.lock` (maintainers).**
  Step 2 used `uv pip install -e`, which resolves `pyproject`'s ranges: it
  never moved an already-installed package to its locked version, so the live
  service drifted from what CI and the e2e venv test (it was five packages
  behind the lock). It now runs `uv sync --locked`, the same locked install
  `setup/onetime_setup.py` and the e2e venv use. The sync is exact, so the
  first deploy also removes a stale `browser-guard` 0.1.0 editable install
  left over from the project's old name. A stale `uv.lock` now fails the
  deploy before the service restarts.

## [1.3.0] — 2026-09-30

### Added
- **`invalidate_dom_cache` tool.** Drops a tab's cached DOM snapshot so the next
  read re-fetches the live HTML, without reloading the page — the fix for a
  change the *page itself* made after the snapshot was taken (its own JS
  revealing a panel, an infinite-scroll batch landing). Unlike `force_reload_tab`
  it issues no page load, so JS-built DOM state survives. It drives no browser
  action and returns no page content, so it isn't read-gated; reads stay gated on
  the tab's live URL at read time. A tab that is gone returns the usual tab-gone
  envelope (#137).
- **Profile-scoped allowlist rules (#139).** A `profiles:` block scopes the
  *whole existing rule grammar* — `denylist`, `read`, and every write action —
  to one browser profile directory, so loosening rules for one kind of work no
  longer loosens them everywhere. A profile's rules are additive over the global
  ones (a profile with no block gets exactly the global rules, so existing
  configs are unchanged), the denylist is unioned and still wins, and `infra`
  stays global and is rejected inside a profile block. Keys are canonicalized
  the way the session layer canonicalizes a caller's `profile_dir`, so
  `~/.cache/browden/p` and its resolved path name one profile; a relative key
  fails the load rather than sitting inert. Per-profile edits hot-reload like
  every other rule (#143).
- **`allow_all: true` inside a profile.** Opens every write action and
  every read *in that profile*, so an agent can be pointed at a fresh scratch
  profile and work without a config edit per host. The Tranco popularity check
  stays **on** under it — turned on for the profile even if the global config
  had it off — so broad browsing covers the established web while an unranked
  host still needs a deliberate act (`read: {tranco: {enabled: false}}` in the
  same block, or a named override). It never enables `file://` or plaintext
  `http://`: those still require a host named explicitly in
  `website_overrides`. The denylist still wins. `allow_all` at the top level is
  rejected — it describes one browsing identity, not every profile at once
  (#144).
- **README: scoping rules to a profile.** Documents the `profiles:` block and
  `allow_all`: additive rules, the unioned denylist, inherited `read` settings,
  canonical keys, `infra` staying global, and what `allow_all` keeps (the
  Tranco net and the scheme gate) (#145).

### Changed
- **Unknown top-level config sections are refused.** A section that is not
  `denylist`, `read`, `infra` or a write action (`click`, `write-text`,
  `press-key`) now fails the load with `unknown section '<name>'` and the list
  of allowed ones. Before, any such key was accepted as a write action no gate
  ever consults, so a typo like `clik:` loaded cleanly and silently authorized
  nothing. At startup this is a config error; on hot reload the last-good
  config stays in force, as for any invalid edit.
- **`ActionAllowlist` renamed to `BrowdenRuntimeConfiguration` (internal).** The
  class that holds one loaded config (the `infra` caps plus the rules) now lives
  in `validator/runtime_configuration.py`; `AllowlistRefresher` and
  `load_allowlist()` became `RuntimeConfigurationRefresher` and
  `load_runtime_configuration()`. Names that refer to the config *file* are
  unchanged (`allowlist.yaml`, `--allowlist`, `$BROWDEN_ALLOWLIST`), as are
  error messages. Maintainers: the first deploy that pulls this fails at step 3,
  because bash keeps running the already-open old copy of the deploy script,
  which calls `load_allowlist`. It aborts before the restart; re-run it (#150).
- **Access rules split out of the runtime configuration (internal).**
  `BrowdenAccessRuleSet` (`validator/access_rule_set.py`) now holds the
  denylist, the read gate and the write-action rules; `BrowdenRuntimeConfiguration`
  keeps the process-wide `infra` caps and reaches the rules through
  `.access_rules`. Write actions are read by name from `WRITE_ACTIONS` rather
  than by excluding reserved keys, so a new top-level section can never be
  mistaken for one. No config behaves differently; groundwork for per-profile
  rules (#139) (#140).
- **Gates are handed the access rules, not the whole configuration (internal).**
  `ensure_url_allowed`, `check_action_host` and the `validate_*_target` gates
  take a `BrowdenAccessRuleSet`, and `BrowdenRuntimeConfiguration` no longer
  forwards `read_policy` / `denylist` / `is_denied` / `section` / `rules_for`:
  `.access_rules` is the only route to a decision. No gate's order or outcome
  changes; groundwork for handing each request its own profile's rules (#139)
  (#141).
- **One profile-path canonicalizer, one per-section schema check (internal).**
  `common.profile.canonical_profile_dir` is the `expanduser().resolve()` the
  server applies to a caller's `profile_dir`, now shared so anything else that
  names a profile reduces it identically. The schema's per-key checks are
  lifted into `_check_section` (with `_check_infra` / `_check_write_action`),
  which is also where an unknown section is refused. No config behaves
  differently; groundwork for profile-scoped rules (#139) (#142).
- **`Allowlist` renamed to `HostRuleMatcher` (internal).** The class that matches
  a (host, page) against per-host page rules backs the denylist and the read
  overrides as well as the write actions, so "allowlist" misdescribed it. Its
  factories (`create_allowlist` / `create_denylist`), module and behavior are
  unchanged (#154).
- **Schema test for a mistyped section inside a profile (internal).** Pins that
  `profiles: {<dir>: {clik: ...}}` fails the load with
  `profiles.<dir>: unknown section 'clik'`, as a top-level typo does (#153).
- **Deploy script's post-deploy e2e runs in a private temp dir (maintainers).**
  `release_new_version.sh` points the e2e run's `TMPDIR` at a fresh
  `browden-e2e.*` dir, so pytest's `tmp_path` dirs and Chrome's scratch dirs
  land there instead of `/tmp`, where they had filled the root filesystem. The
  dir is removed after a green run and kept (its path in the failure message)
  after a red one; each run first sweeps leftover `browden-e2e.*` dirs. The live
  service's Chrome never sees this `TMPDIR` (#151).
- **Deploy script's e2e venv is no longer kept between runs (maintainers).**
  `release_new_version.sh` used to build the post-deploy e2e venv at
  `~/.cache/browden/e2e-venv` and keep it forever (~110 MB). It now lives in
  the run's private `browden-e2e.*` temp dir, so it is removed after a green run
  and kept with the logs after a red one until the next run sweeps it. Setting
  `BROWDEN_E2E_VENV` still gives a persistent venv, which the script never
  removes.
- **Release skill: the version-bump PR is built in a temporary worktree
  (maintainers).** `maintainers/github-release/SKILL.md` now says to prepare
  the `release: cut vX.Y.Z` PR in its own `git worktree` (never the release
  worktree, which must stay clean for the deploy), relock with
  `uv lock --offline`, keep that worktree until the PR merges, and then remove
  it along with the local `release/v*` branch. A later run's preflight clears
  any leftover one.

### Fixed
- **`mcp` is pinned below 2.** The dependency was `mcp[cli]>=1.0` with no
  upper bound, so a fresh install resolved to mcp 2.x, which renamed `FastMCP`
  and cannot import browden at all. It is now `mcp[cli]>=1.0,<2`; `uv.lock`
  still pins 1.28.1, so locked installs are unchanged.
- **Deploy script's e2e venv is built from `uv.lock` (maintainers).** It was
  built with `uv pip install -e`, which ignores the lock and resolves every
  dependency to its newest release, so the post-deploy e2e tested a set of
  packages nothing else runs. It now uses `uv sync --locked --extra dev`, the
  same install CI uses, and fails if the lock is stale.

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

[Unreleased]: https://github.com/nishantsny/browden/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/nishantsny/browden/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/nishantsny/browden/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/nishantsny/browden/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/nishantsny/browden/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/nishantsny/browden/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/nishantsny/browden/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/nishantsny/browden/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/nishantsny/browden/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/nishantsny/browden/releases/tag/v0.1.0
