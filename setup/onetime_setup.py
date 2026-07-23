#!/usr/bin/env python3
"""One-time setup for Browden — cross-platform (Linux, macOS, Windows).

Run it with any Python — `python3 setup/onetime_setup.py`; no venv needed first.

What it does, in order:
  1. Creates a venv at <repo>/.venv and installs browden into it
     (via `uv`, falling back to stdlib venv + pip). Pass --python to use an
     existing interpreter instead and skip this step.
  2. Copies configs/samples/read_only_on_popular_websites.yaml to
     <config-dir>/allowlist.yaml (default ~/.browden) — skipped if a config is
     already there.
  3. Fetches the Tranco top-sites snapshot to <config-dir> — skipped if it is
     already there. The snapshot is not committed; refresh it later with
     setup/fetch_tranco.py.
  4. In --mode stdio (the default), no service is installed — the agent
     launches the server itself over stdio, on demand.
     In --mode service, installs a background service that serves SSE on
     <port>, pinned to that venv, using the host's native service manager:
     systemd (Linux), launchd (macOS), or Task Scheduler (Windows).
  5. Prints the JSON block to add to your agent's settings by hand, then two
     hardening notes: the permission rules that keep the agent from editing any
     browden file, and how to make those files root-owned/read-only so a shell
     or script can't rewrite what the rules only ask about.

Run it again anytime: the venv/install and config copy are idempotent and the
service steps re-apply cleanly. Use --service-name/--port to stand up a second
instance without disturbing an existing one.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# Sibling modules in setup/; stdlib-only, so importing them needs no venv.
from fetch_psl import fetch as fetch_psl, snapshot_path as psl_snapshot_path
from fetch_tranco import DEFAULT_TOP_N, fetch, snapshot_path
from installers import (  # noqa: F401 — installers/_pythonw_for re-exported for tests
    LinuxSystemdInstaller, MacLaunchdInstaller, ServiceInstaller,
    WindowsTaskInstaller, _pythonw_for, _run, select_installer_cls,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ALLOWLIST = REPO_ROOT / "configs" / "samples" / "read_only_on_popular_websites.yaml"
DEFAULT_PORT = 22001  # usually unused; well clear of dev servers on 8000/3000
DEFAULT_CONFIG_DIR = "~/.browden"
DEFAULT_SERVICE_NAME = "browden"
DEFAULT_VENV = REPO_ROOT / ".venv"

def _venv_python(venv_dir: Path) -> Path:
    """The interpreter path inside a venv — layout differs on Windows.

    POSIX venvs put it at ``bin/python``; Windows at ``Scripts/python.exe``.
    """
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def ensure_venv(venv_dir: Path) -> Path:
    """Create ``venv_dir`` (if absent) and install browden into it editable.

    Prefers ``uv sync --frozen``, which creates the venv itself and installs the
    exact dependency set pinned in the committed ``uv.lock`` — so every install
    is reproducible and can't break because a dependency shipped a new release.
    Falls back to the stdlib ``venv`` + ``pip`` (range-resolved, not pinned) when
    uv isn't on PATH. Idempotent — an existing venv is reused and the (fast)
    reinstall just refreshes it. Returns the venv's Python interpreter, which
    the service will run.
    """
    python = _venv_python(venv_dir)
    uv = shutil.which("uv")
    if uv:
        # UV_PROJECT_ENVIRONMENT points uv sync at the chosen venv path (it
        # defaults to <repo>/.venv, which is also our default --venv).
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir)}
        _run([uv, "sync", "--frozen", "--project", str(REPO_ROOT)], env=env)
    else:
        print("[info] uv not found on PATH — using stdlib venv + pip "
              "(unpinned; install uv for the locked, reproducible set)")
        if not python.exists():
            _run([sys.executable, "-m", "venv", str(venv_dir)])
        _run([str(python), "-m", "pip", "install", "-e", str(REPO_ROOT)])
    print(f"[ok]   browden installed in {venv_dir}")
    return python


def copy_config(config_dir: Path) -> Path:
    dest = config_dir / "allowlist.yaml"
    if dest.exists():
        print(f"[skip] {dest} already exists — leaving it untouched")
        return dest
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SAMPLE_ALLOWLIST, dest)
    print(f"[ok]   copied sample allowlist to {dest}")
    return dest


def ensure_tranco(config_dir: Path, top_n: int, allowlist: Path) -> Path:
    """Download the Tranco snapshot next to the allowlist, unless it's there.

    Best-effort: a failed download (offline, Tranco unreachable) warns and moves
    on rather than aborting setup — the read gate degrades to "Tranco matches
    nothing" until you re-run setup/fetch_tranco.py, and the denylist plus any
    website_overrides still apply. On success, records the list id + checksum in
    ``allowlist``'s provenance block.
    """
    dest = snapshot_path(config_dir)
    if dest.exists():
        print(f"[skip] {dest} already exists — leaving it untouched")
        return dest
    try:
        fetch(top_n, dest, allowlist_path=allowlist)
    except Exception as e:  # network error, bad zip, etc. — never fatal to setup
        print(f"[warn] could not fetch Tranco snapshot ({e}); Tranco read-allowlisting "
              f"is inert until you run: python3 setup/fetch_tranco.py")
    return dest


def ensure_psl(config_dir: Path, allowlist: Path) -> Path:
    """Download the Public Suffix List next to the allowlist, unless it's there.

    The read gate reduces a host to its registrable domain with the PSL before
    testing Tranco, so shared-hosting subdomains can't inherit a provider's rank.
    Best-effort: a failed download warns and moves on — the gate falls back to
    publicsuffix2's (older) bundled list until you run setup/fetch_psl.py. On
    success, records the list checksum in ``allowlist``'s provenance block.
    """
    dest = psl_snapshot_path(config_dir)
    if dest.exists():
        print(f"[skip] {dest} already exists — leaving it untouched")
        return dest
    try:
        fetch_psl(dest, allowlist_path=allowlist)
    except Exception as e:  # network error, bad body — never fatal to setup
        print(f"[warn] could not fetch the Public Suffix List ({e}); the read gate "
              f"falls back to publicsuffix2's bundled list until you run: python3 setup/fetch_psl.py")
    return dest


# --- agent config blocks ----------------------------------------------------

def sse_config(service_name: str, port: int) -> str:
    return json.dumps({
        "mcpServers": {
            service_name: {"type": "sse", "url": f"http://127.0.0.1:{port}/sse"}
        }
    }, indent=2)


def stdio_config(service_name: str, python: str, allowlist: Path,
                 display: str | None) -> str:
    entry: dict = {
        "command": python,
        "args": ["-m", "browden.mcp.server", "--allowlist", str(allowlist)],
    }
    if display:  # X11 only; irrelevant (and omitted) on macOS/Windows
        entry["env"] = {"DISPLAY": display}
    return json.dumps({"mcpServers": {service_name: entry}}, indent=2)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("stdio", "service"), default="stdio",
                        help="stdio (default): no service — the agent launches the "
                             "server itself. service: install a background SSE service "
                             "via the host's native manager (systemd/launchd/Task "
                             "Scheduler).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"SSE port for --mode service (default: {DEFAULT_PORT})")
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR,
                        help=f"where the allowlist config lives (default: {DEFAULT_CONFIG_DIR})")
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME,
                        help=f"service/task name (default: {DEFAULT_SERVICE_NAME})")
    parser.add_argument("--venv", default=str(DEFAULT_VENV),
                        help=f"venv to create/use for the service (default: {DEFAULT_VENV})")
    parser.add_argument("--python", default=None,
                        help="use an existing interpreter for the service and skip venv "
                             "creation (default: create/use --venv)")
    parser.add_argument("--display", default=os.environ.get("DISPLAY", ":0"),
                        help="DISPLAY for headed Chrome on Linux/X11 (default: current, else :0)")
    parser.add_argument("--tranco-top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"how many top Tranco domains to fetch (default: {DEFAULT_TOP_N})")
    args = parser.parse_args(argv)

    if not SAMPLE_ALLOWLIST.exists():
        raise SystemExit(f"sample config missing: {SAMPLE_ALLOWLIST} — is the repo intact?")

    config_dir = Path(args.config_dir).expanduser().resolve()
    allowlist = copy_config(config_dir)
    ensure_tranco(config_dir, args.tranco_top_n, allowlist)
    ensure_psl(config_dir, allowlist)

    if args.python:
        service_python = args.python
        print(f"[ok]   using existing interpreter {service_python} (skipping venv)")
    else:
        service_python = str(ensure_venv(Path(args.venv).expanduser().resolve()))

    # DISPLAY only matters for headed Chrome on Linux/X11.
    display = args.display if sys.platform.startswith("linux") else None

    if args.mode == "stdio":
        agent_json = stdio_config(args.service_name, service_python, allowlist, display)
        transport_note = (
            "\nDone (stdio mode). No background service was installed — your agent "
            "launches the server on demand. Add this to your agent's settings by hand "
            "(e.g. ~/.claude.json or .gemini/settings.json):\n\n" + agent_json
        )
    else:
        installer = select_installer_cls()(
            service_name=args.service_name, port=args.port, allowlist=allowlist,
            python=service_python, display=args.display, repo_root=REPO_ROOT,
            config_dir=config_dir)
        installer.install()
        agent_json = sse_config(args.service_name, args.port)
        transport_note = (
            f"\nDone ({installer.manager} service on port {args.port}). Add this to "
            f"your agent's settings by hand (e.g. ~/.claude.json or "
            f".gemini/settings.json):\n\n{agent_json}"
        )

    print(transport_note)
    print(
        f"\nThe Tranco top-sites snapshot the read gate uses lives at "
        f"{snapshot_path(config_dir)} (not committed). Refresh it anytime with:\n\n"
        f"    python3 setup/fetch_tranco.py --config-dir {config_dir}\n"
    )
    print(guard_files_note(config_dir, allowlist, REPO_ROOT))
    print(lockdown_note(config_dir, REPO_ROOT))


def _rule_path(path: Path) -> str:
    """Render ``path`` the way agent permission rules want it spelled.

    Claude Code matches file rules gitignore-style: ``~/`` for a home-relative
    path, a leading ``//`` for one anchored at the filesystem root. Anything
    else would be read as relative to the settings file.
    """
    home = Path.home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return "//" + path.as_posix().lstrip("/")


def guard_files_note(config_dir: Path, allowlist: Path, repo_root: Path) -> str:
    """Advisory text nudging the user to gate agent edits to *every* browden file.

    The allowlist is the security boundary, but it is not the only file that
    decides what the agent may do: the Tranco and PSL snapshots feed the read
    gate, and browden's own source is what enforces all of it. An agent that can
    silently rewrite any of them can widen its own read/click permissions. So
    the agent's settings should refuse those edits outright ("deny"), or at the
    very least require approval ("ask") before every one — never auto-approve.
    Returned as a string (not printed) so it stays easy to test and reuse.
    """
    rules = [f'"Edit({_rule_path(config_dir)}/**)"']   # allowlist + Tranco/PSL snapshots
    if allowlist.parent != config_dir:                 # an allowlist kept elsewhere
        rules.append(f'"Edit({_rule_path(allowlist)})"')
    rules.append(f'"Edit({_rule_path(repo_root)}/**)"')  # the code enforcing the policy
    body = ",\n          ".join(rules)
    return (
        f"\nSecurity tip (recommended): every browden file is part of the "
        f"perimeter — {allowlist} decides what the agent may visit and click, "
        f"the Tranco/PSL snapshots in {config_dir} feed the read gate, and the "
        f"code in {repo_root} enforces both. Stop the agent from quietly "
        f"widening its own access by denying edits to all of them in your "
        f'agent\'s settings (swap "deny" for "ask" if you do want to edit them '
        f"through the agent, with approval every time — never auto-approve). "
        f"For Claude Code, in ~/.claude/settings.json:\n\n"
        f"    {{\n"
        f'      "permissions": {{\n'
        f'        "deny": [\n'
        f"          {body}\n"
        f"        ]\n"
        f"      }}\n"
        f"    }}\n"
    )


def lockdown_note(config_dir: Path, repo_root: Path) -> str:
    """Advisory: permission rules bind file tools only — the OS is the real lock.

    Those rules constrain the agent's *edit* tools. They do not constrain what a
    shell or a script does once launched: ``Bash``, a python one-liner, an
    editor, or any subprocess can rewrite the same files, and command rules are
    matched on text that is trivially rephrased (``sed -i``, ``sh -c``, ``tee``,
    a here-doc). The only enforcement that holds regardless of how the write is
    spelled is filesystem permissions — make browden's files root-owned and
    read-only to everyone else, so no unprivileged process can touch them.
    """
    posix = (
        f"    sudo chown -R root:root {config_dir}\n"
        f"    sudo find {config_dir} -type d -exec chmod 755 {{}} +\n"
        f"    sudo find {config_dir} -type f -exec chmod 444 {{}} +\n"
    )
    windows = (
        f"    icacls \"{config_dir}\" /inheritance:r "
        f'/grant "Administrators:(OI)(CI)F" /grant "Users:(OI)(CI)RX" /T\n'
    )
    cmds = windows if os.name == "nt" else posix
    return (
        "\nAdvisory (the rules above are not a lock): agent permission rules "
        "only gate the agent's own file tools. Any shell it can run — Bash, a "
        "`python -c`, `sed -i`, an editor — writes to these files directly, and "
        "command-matching rules are easy to sidestep by rephrasing the command. "
        "Treat the rules as a speed bump.\n"
        "\nFor an enforced boundary, hand the files to root and leave everyone "
        "else read-only — browden only ever reads them:\n\n"
        f"{cmds}"
        "\n  Directories keep their execute bit (755): on a directory that is "
        "the traverse permission, and dropping it would hide the files from "
        "browden too. The data files need no execute bit at all (444). Root "
        "ownership of the directory is what matters — it is what stops a "
        "non-root process from replacing a file it cannot write.\n"
        f"\n  Do the same for the install tree ({repo_root}) if you never edit "
        "browden's code — but not while developing it, and note that a venv "
        "owned by root can no longer be updated without sudo.\n"
        "\n  After locking down, refreshing the snapshots needs sudo:\n\n"
        f"    sudo python3 setup/fetch_tranco.py --config-dir {config_dir}\n"
    )


if __name__ == "__main__":
    main()
