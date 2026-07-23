#!/usr/bin/env bash
#
# deploy.sh — redeploy browden to the live systemd --user service from the deploy
# branch (origin/main by default), then run the e2e suite as a post-deploy smoke test.
#
# Pipeline (fails fast — any step aborts with a non-zero exit):
#   0. preflight  — release checkout / venv / config / systemctl all present
#   1. fast-forward the release checkout to origin/$BROWDEN_DEPLOY_BRANCH (default main)
#   2. sync the release venv to the pulled pyproject deps (uv pip install -e),
#      so a ff-pull that ADDED a runtime dependency doesn't crash-loop the
#      service on import — same editable install setup/onetime_setup.py does
#   3. validate the config loads under the JUST-PULLED code (before we restart,
#      so a config the new schema rejects fails fast instead of crash-looping)
#   4. restart the service + health-check (active AND listening on the port)
#   5. run the e2e suite from the deployed tree (post-deploy verification)
#
# Idempotent: safe to re-run even when already at the deploy branch (it redeploys).
#
# The Tranco snapshot lives beside the config in ~/.browden (NOT in the checkout),
# so the release tree stays clean and the ff-pull just works — see the deploy
# notes. Untracked files in the release tree are ignored; only local *tracked*
# edits (which would block the ff-pull) abort the deploy.
#
# Overridable via env (defaults match the live host):
#   BROWDEN_RELEASE_DIR  release checkout to deploy FROM  ($HOME/projects/browser-guard-release)
#   BROWDEN_DEPLOY_BRANCH  branch to deploy (default main); the checkout must be ON
#                          this branch. Lets you smoke-test a feature branch on the
#                          live service before merge; --release stays main-only.
#   BROWDEN_ALLOWLIST    allowlist config the service loads (~/.browden/allowlist.yaml)
#   BROWDEN_SERVICE      systemd --user unit name           (browden.service)
#   BROWDEN_PORT         port to health-check              (derived from the unit's MCP_PORT)
#   BROWDEN_E2E_VENV     cached venv used to run e2e         (~/.cache/browden/e2e-venv)
#
# Usage:
#   ~/scripts/deploy.sh                          # deploy + run the whole e2e suite
#   ~/scripts/deploy.sh test/e2e/test_tabs.py    # deploy + run only these e2e tests
#   ~/scripts/deploy.sh --release=v1.0.1         # deploy + e2e, THEN cut the release
#
# --release=<tag> is the ONLY way the GitHub release steps run. gh presence + auth
# are checked in preflight (start of the run) when --release is set, so an auth
# problem fails in seconds rather than after the ~7-minute deploy + e2e. After a
# fully green deploy + e2e (guarded on $deploy_ok), it fail-closed verifies the
# release preconditions (release tree == origin/main, the tag's version has a
# CHANGELOG.md section, pyproject matches, the tag doesn't already exist), then
# annotates + pushes the tag and watches the `release` workflow (which fast-forwards
# `stable` and cuts the GitHub Release) with a 10-minute timeout, reporting
# success/failure/timeout. Any precondition failing aborts BEFORE the tag is
# created — nothing is pushed.
set -euo pipefail

RELEASE_DIR="${BROWDEN_RELEASE_DIR:-$HOME/projects/browser-guard-release}"
CONFIG="${BROWDEN_ALLOWLIST:-$HOME/.browden/allowlist.yaml}"
SERVICE="${BROWDEN_SERVICE:-browden.service}"
# Branch to deploy. Defaults to main; override to smoke-test a feature branch on
# the live service before it merges (the checkout must be on that branch). A
# GitHub release (--release) is refused for anything but main — see preflight.
DEPLOY_BRANCH="${BROWDEN_DEPLOY_BRANCH:-main}"
# Health-check the port the live unit actually binds — read MCP_PORT from its
# Environment rather than hardcode a literal, so this can never drift from the
# service. Overridable via BROWDEN_PORT; validated (non-empty) in preflight.
PORT="${BROWDEN_PORT:-$(systemctl --user show "$SERVICE" -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^MCP_PORT=//p' | head -1)}"
VENV_PY="$RELEASE_DIR/.venv/bin/python"

# Split out --release=<tag>; everything else is passed through to pytest as e2e args.
RELEASE_TAG=""
E2E_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --release=*) RELEASE_TAG="${arg#--release=}" ;;
        *)           E2E_ARGS+=("$arg") ;;
    esac
done
[ "${#E2E_ARGS[@]}" -gt 0 ] || E2E_ARGS=(test/e2e)

# Set to 1 only after a fully green deploy + e2e (just above the release stage).
# The release stage is guarded on it, so even if a future edit weakened the
# fail-fast (set -e / `|| die`) above, the tag stage can never run after a failure.
deploy_ok=0

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---- 0. preflight ---------------------------------------------------------
log "Preflight"
[ -d "$RELEASE_DIR" ] || die "release checkout dir not found: $RELEASE_DIR"
# .git is a *file* in a git worktree, so ask git rather than test for a dir.
git -C "$RELEASE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "not a git checkout: $RELEASE_DIR"
[ -x "$VENV_PY" ]          || die "release venv python missing: $VENV_PY"
[ -f "$CONFIG" ]           || die "allowlist config missing: $CONFIG"
command -v systemctl >/dev/null || die "systemctl not found"
command -v ss >/dev/null        || die "ss (iproute2) not found"
command -v uv >/dev/null        || die "uv not found (needed to build the e2e venv)"
[ -n "$PORT" ] \
    || die "could not determine $SERVICE port (no MCP_PORT in its unit Environment) — set BROWDEN_PORT"

# When cutting a release, verify gh is present + authenticated NOW — at the start,
# not after a ~7-minute deploy + e2e — so an auth problem fails in seconds instead
# of wasting the whole run only to die at the tag step.
if [ -n "$RELEASE_TAG" ]; then
    command -v gh >/dev/null || die "gh CLI not found (needed to cut/watch the release)"
    command -v timeout >/dev/null || die "timeout (coreutils) not found (needed to bound the release watch)"
    gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run 'gh auth login'"
fi

branch="$(git -C "$RELEASE_DIR" rev-parse --abbrev-ref HEAD)"
[ "$branch" = "$DEPLOY_BRANCH" ] \
    || die "release checkout is on '$branch', not '$DEPLOY_BRANCH' (BROWDEN_DEPLOY_BRANCH) — refusing to deploy"

# Refuse to deploy on top of local *tracked* edits (they'd block the ff-pull).
[ -z "$(git -C "$RELEASE_DIR" status --porcelain --untracked-files=no)" ] \
    || die "release tree has local tracked changes — refusing to deploy. Inspect: git -C '$RELEASE_DIR' status"

before="$(git -C "$RELEASE_DIR" rev-parse --short HEAD)"

# ---- 1. fast-forward to origin/$DEPLOY_BRANCH ----------------------------
log "Fetching + fast-forwarding $RELEASE_DIR to origin/$DEPLOY_BRANCH"
git -C "$RELEASE_DIR" fetch --quiet origin "$DEPLOY_BRANCH"
git -C "$RELEASE_DIR" merge --ff-only "origin/$DEPLOY_BRANCH"
after="$(git -C "$RELEASE_DIR" rev-parse --short HEAD)"
if [ "$before" = "$after" ]; then
    log "Already at origin/$DEPLOY_BRANCH ($after) — no new commits; redeploying anyway"
else
    log "Advanced $before -> $after"
    git -C "$RELEASE_DIR" --no-pager log --oneline "$before..$after"
fi

# ---- 2. sync the release venv to the pulled pyproject deps ----------------
# The service runs from the source tree, but its venv must carry the declared
# runtime dependencies. A ff-pull that adds a new one (e.g. publicsuffix2) would
# otherwise crash-loop the service on import. Editable + idempotent, this mirrors
# ensure_venv() in setup/onetime_setup.py (runtime deps only — no [dev] extras,
# keeping the production venv lean). Runs BEFORE config-validate/restart so both
# execute against the synced venv.
log "Syncing release venv to pyproject runtime deps (uv pip install -e)"
uv pip install --quiet --python "$VENV_PY" -e "$RELEASE_DIR"

# ---- 3. validate the config against the just-pulled code ------------------
log "Validating $CONFIG under the release venv"
"$VENV_PY" - "$CONFIG" <<'PY'
import sys
from browden.configs.loader import load_allowlist
load_allowlist(sys.argv[1])
print("config loads OK")
PY

# ---- 4. restart + health check -------------------------------------------
log "Restarting $SERVICE"
systemctl --user restart "$SERVICE"

log "Waiting for $SERVICE to listen on 127.0.0.1:$PORT"
for _ in $(seq 1 20); do
    if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then break; fi
    sleep 0.5
done
systemctl --user is-active --quiet "$SERVICE" \
    || { journalctl --user -u "$SERVICE" -n 30 --no-pager; die "$SERVICE is not active after restart"; }
ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN \
    || { journalctl --user -u "$SERVICE" -n 30 --no-pager; die "$SERVICE is not listening on $PORT"; }
log "$SERVICE is active and listening on $PORT (now running $after)"

# ---- 5. post-deploy e2e smoke test ---------------------------------------
# The live service venv is runtime-only (no test deps), so run e2e from a
# dedicated, cached venv into which the release tree is installed EDITABLE —
# pytest therefore exercises the just-deployed source, and the production venv is
# never mutated. The harness spawns its OWN isolated Chrome/profile
# (XDG_CACHE_HOME under a tmpdir), so it never touches the live service's profile.
# BROWDEN_HEADLESS=1 is exported in the real env because the subprocess-spawning
# harness tests don't inherit conftest's monkeypatched value.
E2E_VENV="${BROWDEN_E2E_VENV:-$HOME/.cache/browden/e2e-venv}"
log "Preparing e2e venv ($E2E_VENV): release tree + dev extras"
[ -x "$E2E_VENV/bin/python" ] || uv venv "$E2E_VENV"
uv pip install --quiet --python "$E2E_VENV/bin/python" -e "$RELEASE_DIR[dev]"

log "Running e2e (headless) from $RELEASE_DIR: ${E2E_ARGS[*]}"
cd "$RELEASE_DIR"
BROWDEN_HEADLESS=1 "$E2E_VENV/bin/python" -m pytest "${E2E_ARGS[@]}" -q \
    || die "e2e FAILED — the new code is LIVE but did not pass e2e. Investigate or roll back to $before."

log "DEPLOY OK — $SERVICE live at $after, e2e green."
deploy_ok=1   # deploy + e2e passed; the release stage may now proceed

# ---- 6. cut a GitHub release (ONLY with --release=<tag>) -------------------
# Reached only after a fully green deploy + e2e above (guarded on $deploy_ok, and
# on set -e / `|| die` before it). Every check here is fail-closed: on any failure
# we die BEFORE creating the tag, so a bad release never gets pushed. cwd is
# $RELEASE_DIR (the e2e step cd'd here), so git/gh act on the browden repo.
# gh presence + auth were already verified in preflight (fail-fast, no wasted run).
if [ -n "$RELEASE_TAG" ] && [ "$deploy_ok" = 1 ]; then
    log "Release: verifying preconditions for $RELEASE_TAG"
    ver="${RELEASE_TAG#v}"
    ver_re="$(printf '%s' "$ver" | sed 's/\./\\./g')"

    # We release origin/main. The ff-pull above already put HEAD there; re-assert it.
    git -C "$RELEASE_DIR" fetch --quiet origin main
    head_sha="$(git -C "$RELEASE_DIR" rev-parse HEAD)"
    [ "$head_sha" = "$(git -C "$RELEASE_DIR" rev-parse origin/main)" ] \
        || die "release tree HEAD is not origin/main — refusing to tag"

    # The tag's version MUST be described in the changelog and match pyproject, on
    # this very commit (so the release and its notes agree, and the workflow's own
    # version gate will pass).
    grep -qE "^## \[${ver_re}\]" "$RELEASE_DIR/CHANGELOG.md" \
        || die "CHANGELOG.md has no '## [$ver]' section — add it and merge to main before releasing"
    pyver="$(sed -nE 's/^version = "([^"]+)"/\1/p' "$RELEASE_DIR/pyproject.toml" | head -1)"
    [ "$pyver" = "$ver" ] || die "pyproject version ($pyver) != release version ($ver)"

    # Refuse to clobber an existing tag.
    if git -C "$RELEASE_DIR" rev-parse -q --verify "refs/tags/$RELEASE_TAG" >/dev/null 2>&1 \
       || git -C "$RELEASE_DIR" ls-remote --exit-code --tags origin "$RELEASE_TAG" >/dev/null 2>&1; then
        die "tag $RELEASE_TAG already exists (local or remote) — bump the version or remove the stale tag"
    fi

    log "Tagging $RELEASE_TAG at $head_sha and pushing (triggers the release workflow)"
    git -C "$RELEASE_DIR" tag -a "$RELEASE_TAG" -m "$RELEASE_TAG"
    git -C "$RELEASE_DIR" push origin "$RELEASE_TAG"

    log "Waiting for the release workflow run to appear"
    run_id=""
    for _ in $(seq 1 18); do
        run_id="$(gh run list --workflow=release.yml -L 15 \
                    --json databaseId,headBranch \
                    --jq ".[] | select(.headBranch==\"$RELEASE_TAG\") | .databaseId" 2>/dev/null | head -1)"
        [ -n "$run_id" ] && break
        sleep 5
    done
    [ -n "$run_id" ] || die "release workflow run for $RELEASE_TAG did not appear — check GitHub Actions (the tag IS pushed)"

    # Watch the release job, bounded to 10 minutes. Three outcomes, reported
    # explicitly: succeeded (rc 0), failed (rc from --exit-status), or timed out
    # (timeout's rc 124). The tag is already pushed in every non-success case.
    log "Watching release workflow run $run_id (10-minute timeout)"
    watch_rc=0
    timeout 600 gh run watch "$run_id" --exit-status || watch_rc=$?
    if [ "$watch_rc" -eq 124 ]; then
        die "release workflow did not finish within 10 min for $RELEASE_TAG (timed out) — the tag IS pushed; inspect: gh run view $run_id"
    elif [ "$watch_rc" -ne 0 ]; then
        die "release workflow FAILED for $RELEASE_TAG (watch exit $watch_rc) — inspect: gh run view $run_id --log-failed (the tag is pushed)"
    fi

    # Post-watch: independently confirm the run's RECORDED conclusion is 'success'
    # — not merely that `gh run watch` exited 0 — before declaring the release done.
    # Anything else (failure, cancelled, timed_out, or empty/unknown) is a failure.
    conclusion="$(gh run view "$run_id" --json conclusion --jq '.conclusion' 2>/dev/null || echo "")"
    [ "$conclusion" = "success" ] \
        || die "release workflow run $run_id did not conclude success (got: ${conclusion:-unknown}) for $RELEASE_TAG — the tag is pushed; inspect: gh run view $run_id --log-failed"
    log "Release workflow $run_id concluded: success"

    stable_sha="$(git -C "$RELEASE_DIR" ls-remote origin refs/heads/stable | cut -f1)"
    rel_url="$(gh release view "$RELEASE_TAG" --json url --jq .url 2>/dev/null || echo '(release page)')"
    log "RELEASE OK — $RELEASE_TAG published; stable now $stable_sha; $rel_url"
fi
