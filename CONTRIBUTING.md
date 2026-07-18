# Contributing to browden

Thanks for your interest in improving browden! Contributions of all kinds are
welcome — bug reports, docs, allowlist samples, and code.

## Ground rules

- Be excellent to each other — see the [Code of Conduct](./CODE_OF_CONDUCT.md).
- **Found a security issue? Do not open a public issue.** Follow the
  [Security Policy](./SECURITY.md) instead.
- By contributing, you agree your contributions are licensed under the project's
  [Apache License 2.0](./LICENSE).

## Development setup

Requires **Python ≥ 3.11** and **Google Chrome** on the host.

```bash
uv sync --extra dev                      # locked install from uv.lock (or: python -m venv .venv && pip install -e ".[dev]")
uv run pytest test/unit/                 # fast; never launches a browser
uv run pytest test/e2e/                  # drives a real Chrome
BROWDEN_HEADLESS=1 uv run pytest test/e2e/   # on a machine with no display
```

Dependencies are pinned in the committed `uv.lock`. If you change
`pyproject.toml` dependencies, run `uv lock` and commit the updated lockfile —
CI installs with `--frozen` and fails if the two drift apart.

Unit tests never touch a browser and run on Linux/macOS/Windows. The e2e suite
renders inline `data:` pages in a throwaway profile (no network, no allowlisted
host) and stands the real MCP server up on an ephemeral port.

## Making a change

1. **Open an issue first** for anything non-trivial, so we can agree on the
   approach before you invest time.
2. Branch off `main`.
3. Keep the change focused; match the style, naming, and comment density of the
   surrounding code. browden annotates types everywhere and prefers required
   fields over optional-with-`None` defaults.
4. **Add or update tests.** New behavior needs a unit test at minimum; anything
   touching the browser path should have e2e coverage.
5. Run `pytest test/unit` (and `test/e2e` if you touched the browser path)
   locally before pushing.
6. Open a PR against `main`. CI runs unit tests on Linux/macOS/Windows and the
   e2e suite on headless Chrome — both must pass.

## Scope: what browden is (and isn't)

browden is deliberately a **narrow, read-first safety perimeter**. Write actions
(`click`, `insert_text`) are default-deny and gated per host + per visible label.
When proposing a new tool or a new write action, keep it consistent with that
model: small, audited, allowlist-and-label gated. Features that turn browden into
a general "drive the browser" automation tool are out of scope by design — the
README's "When NOT to use browden" table points to better tools for that.

## Maintaining dependencies (maintainers)

`uv.lock` pins the resolved dependency set. The lockfile is *enforced* only at
the two edges that should reproduce it rather than re-resolve; everywhere else
stays free to change:

- **Bump a package** — edit `pyproject.toml`, run `uv sync` (re-resolves and
  rewrites `uv.lock`) or `uv lock --upgrade` (bumps within existing ranges),
  then commit `uv.lock`. Local dev is never `--frozen`, so this always works.
- **`--frozen` lives in exactly two places** — CI (`uv sync --frozen`, so a
  `pyproject.toml` change without a committed `uv lock` fails the build instead
  of drifting) and the end-user installer in `setup/onetime_setup.py` (so users
  get the exact tested set). Re-lock and commit before merging, or CI stays red.
- **uv itself is unpinned** — CI installs the latest uv each run and setup uses
  whatever `uv` is on PATH. `uv.lock` pins packages, not uv; nothing to bump.

## Questions

Open a [discussion or issue](https://github.com/nishantsny/browden/issues) — all
feedback is welcome.
