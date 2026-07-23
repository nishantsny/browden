# Changelog

All notable changes to browden are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Only the latest release is supported
(see [SECURITY.md](./SECURITY.md)).

## [Unreleased]

## [1.0.1] — 2026-07-19

### Added
- **iframe inspection** — new `switch_to_frame`, `switch_to_parent_frame`, and
  `switch_to_default_content` tools focus a tab's browsing context on an `<iframe>`
  so the existing DOM-read tools (`query_selector`, `get_element_by_id`,
  `screenshot`, …) can inspect its contents — which were previously invisible
  (the tools only ever saw the top document). The frame focus is replayed across the
  window-refocus that nearly every op performs, and reset on `navigate`/`reload`
  (#117).

### Security
- `switch_to_frame` is same-origin only and gates the frame as a distinct document:
  the iframe's declared `src` is checked against the read allowlist *before*
  switching, and the frame's actual `document.URL` is gated (read-allowed **and**
  same-origin with the top page) *after* switching. Cross-origin frames are refused
  — the existing click/write host gate keys off the tab's top URL, so it cannot
  govern a different-origin document (that needs a frame-aware write gate, deferred).
  On any failure the driver returns to the top document and nothing is inspected.
- `switch_to_parent_frame` / `switch_to_default_content` **re-verify the landed
  document on every call**, not just on entry: another process may have navigated an
  ancestor (or the top page) to an untrusted URL while we were deeper in the tree, so
  the document returned to is re-gated (read-allowed + same-origin). On refusal the
  driver retreats to the top document and the call raises — mirroring how the read
  tools re-check the live URL on every call, not only on navigate.

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

[Unreleased]: https://github.com/nishantsny/browden/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/nishantsny/browden/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/nishantsny/browden/releases/tag/v0.1.0
