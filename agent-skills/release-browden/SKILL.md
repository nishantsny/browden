name: release-browden
description: >
    Cut a new browden release. Drives `~/scripts/deploy.sh --release=<tag>` to
    deploy the latest origin/main into the release worktree, run the full e2e,
    and — only on a fully green deploy — tag the release and watch the GitHub
    `release` workflow fast-forward `stable` and publish the Release. Asks the
    user for the version/tag and whether to open a CHANGELOG PR, and NEVER tags a
    version that isn't written into CHANGELOG.md and merged on main.
    Trigger on "release browden", "cut a browden release", "/release-browden".

## What this does

**Always** invoke the deploy script **with `--release=<tag>`** — one authoritative
run does everything: fast-forward the release worktree to origin/main, sync the
venv, restart the service, run the full e2e, and THEN — only if every earlier step
passed — verify the release preconditions, tag origin/main, push the tag, and watch
the workflow. There is no separate "deploy-first without --release" phase.

The script is **fail-closed**: any failure in the deploy or e2e steps aborts the
run before the tag is ever created, so a red run never publishes. It also checks
`gh` presence + authentication at the *start* when `--release` is present, so an
auth problem fails in seconds instead of after a ~7-minute e2e.

Because `--release` **publishes** (a public tag + Release, fast-forwarding
`stable`), the version and the CHANGELOG must be settled on main *before* the run
— hence the ordering below: ask + resolve the HARD RULE first, then the single
`--release` run.

## HARD RULE — never violate

Do **not** create or push a tag unless BOTH hold on **origin/main**:

1. `CHANGELOG.md` has a `## [<version>]` section for the tag's version, and
2. `pyproject.toml` `version` equals the tag's version.

The tag being cut must be *referenced in the changelog and checked into main first*.
The deploy script re-checks this and dies before tagging, but verify it in the skill
too so you fail fast (before a ~7-minute run) and can offer to fix it.

## Steps

1. **Preflight (read-only).** Work against the release worktree
   (`${BROWDEN_RELEASE_DIR:-$HOME/projects/browser-guard-release}`).
   `git fetch origin main` and report: current `origin/main` HEAD, `pyproject`
   version, latest `git tag`, the `## [Unreleased]` + newest version section of
   `CHANGELOG.md`, and that `.github/workflows/release.yml` exists on main. If the
   worktree has local *tracked* changes, stop (the deploy would refuse anyway).

2. **Ask the user** (AskUserQuestion) — up front, because the single `--release`
   run needs the tag and a merged changelog before it can start:
   - **Release version / tag** — must be `vX.Y.Z` (e.g. `v1.1.0`). Never pick it
     yourself.
   - **CHANGELOG PR** — if the version isn't already a `## [<version>]` section on
     main, open a PR to add it? (default: Yes/recommended.)

3. **Resolve the HARD RULE.** Against `origin/main`, check for `## [<version>]` in
   `CHANGELOG.md` and `pyproject` `version == <version>`.
   - Both satisfied → continue to step 4.
   - Missing, and the user opted for a PR → create a branch off `origin/main` that
     (a) sets `pyproject` `version` to `<version>` if needed and (b) moves the
     `[Unreleased]` notes under a new `## [<version>] — <today>` heading (fix the
     compare links if present). Open the PR. Then **STOP**: tell the user to merge
     it to main and re-run `/release-browden`. Do not tag — the tag's commit must
     already describe it on main.
   - Missing, and the user declined → **STOP** and explain the release is blocked
     until the version is in the changelog on main.

4. **Cut the release** (only when step 3 is satisfied). Confirm intent with the
   user first — this publishes. Run `~/scripts/deploy.sh --release=<tag>` **once**
   (a real service restart + public tag; the user may prefer to run it themselves
   via `! ~/scripts/deploy.sh --release=<tag>`). The single run: checks gh auth up
   front, then deploys + e2es, and ONLY on green verifies the preconditions again,
   tags `<tag>` at origin/main, pushes it, and watches the `release` workflow that
   fast-forwards `stable` and cuts the GitHub Release. If it fails at deploy or
   e2e, **no tag is pushed** — fix and re-run. The one point of no return is the
   `git push origin <tag>`; after that the tag is out even if the workflow later
   fails.

5. **Report.** Relay the script's `RELEASE OK — …` line, then confirm out of band:
   `git ls-remote --heads origin stable` equals the tag's commit, and
   `gh release view <tag> --json url` shows the published Release. If the script's
   own workflow-watch was interrupted (e.g. the run was backgrounded), watch it
   yourself with `gh run watch <id> --exit-status` before declaring success.

## Notes / gotchas

- This is a **publish** action (a tag + a public Release, awkward to unwind once
  consumers pull `stable`). Confirm the version with the user before the run;
  never pick it yourself.
- Always `--release`: there is no unblocked "deploy only" phase in this skill. A
  plain `~/scripts/deploy.sh` (no tag) is still fine for a routine redeploy, but a
  *release* always goes through the single `--release` run so the tag is only ever
  cut on a fresh, green deploy + e2e.
- The `release` workflow only fires for a tag whose commit contains
  `.github/workflows/release.yml` (true on main since the release-channel change).
- If the run 403s on the stable push / release create, check **repo Settings →
  Actions → General → Workflow permissions = "Read and write"** (and, if `stable` is
  branch-protected, that the workflow is allowed to push it), then `gh run rerun`.
- The deploy script honors `BROWDEN_RELEASE_DIR`, `BROWDEN_SERVICE`,
  `BROWDEN_PORT`, `BROWDEN_ALLOWLIST`, `BROWDEN_E2E_VENV`.
