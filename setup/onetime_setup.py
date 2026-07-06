#!/usr/bin/env python3
"""One-time setup for Browden (SSE transport only; stdio stays manual).

Run it with any Python — `python3 setup/onetime_setup.py`; no venv needed first.

What it does, in order:
  1. Creates a venv at <repo>/.venv and installs browden into it
     (via `uv`, falling back to stdlib venv + pip). Pass --python to use an
     existing interpreter instead and skip this step.
  2. Copies configs/samples/allowlist.yaml to <config-dir>/allowlist.yaml
     (default ~/.browden) — skipped if a config is already there.
  3. Fetches the Tranco top-sites snapshot (top 400k) to
     <config-dir>/tranco-top-400k.txt.gz — skipped if it is already there. The
     snapshot is not committed; refresh it later with setup/fetch_tranco.py.
  4. Writes a systemd *user* unit that serves SSE on <port>, pinned to that venv.
  5. Runs `systemctl --user daemon-reload` and `enable --now <service-name>`.
  6. Prints the JSON block to add to your agent's settings by hand.

Run it again anytime: the venv/install and config copy are idempotent and the
systemd steps re-apply cleanly. Use --service-name/--port to stand up a second
instance without disturbing an existing one.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Sibling module in setup/; stdlib-only, so importing it needs no venv.
from fetch_tranco import DEFAULT_TOP_N, TRANCO_FILENAME, fetch, snapshot_path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ALLOWLIST = REPO_ROOT / "configs" / "samples" / "allowlist.yaml"
DEFAULT_PORT = 22001  # usually unused; well clear of dev servers on 8000/3000
DEFAULT_CONFIG_DIR = "~/.browden"
DEFAULT_SERVICE_NAME = "browden"
DEFAULT_VENV = REPO_ROOT / ".venv"

UNIT_TEMPLATE = """\
[Unit]
Description=Browden MCP Server (SSE, {service_name})
After=network.target

[Service]
Environment=DISPLAY={display}
Environment=MCP_TRANSPORT=sse
Environment=MCP_HOST=127.0.0.1
Environment=MCP_PORT={port}
WorkingDirectory={repo_root}
ExecStart={python} -m browden.mcp.server --allowlist {allowlist}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _run(cmd: list[str]) -> None:
    print(f"[run]  {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def ensure_venv(venv_dir: Path) -> Path:
    """Create ``venv_dir`` (if absent) and install browden into it editable.

    Prefers ``uv``; falls back to the stdlib ``venv`` + ``pip``. Idempotent — an
    existing venv is reused and the (fast) editable reinstall just refreshes it.
    Returns the venv's Python interpreter, which the service will run.
    """
    python = venv_dir / "bin" / "python"
    uv = shutil.which("uv")
    if uv:
        if not python.exists():
            _run([uv, "venv", str(venv_dir)])
        _run([uv, "pip", "install", "--python", str(python), "-e", str(REPO_ROOT)])
    else:
        print("[info] uv not found on PATH — using stdlib venv + pip")
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


def fetch_tranco(config_dir: Path, top_n: int) -> Path:
    """Download the Tranco snapshot next to the allowlist, unless it's there.

    Best-effort: a failed download (offline, Tranco unreachable) warns and moves
    on rather than aborting setup — the read gate degrades to "Tranco matches
    nothing" until you re-run setup/fetch_tranco.py, and the denylist plus any
    website_overrides still apply.
    """
    dest = snapshot_path(config_dir)
    if dest.exists():
        print(f"[skip] {dest} already exists — leaving it untouched")
        return dest
    try:
        fetch(top_n, dest)
    except Exception as e:  # network error, bad zip, etc. — never fatal to setup
        print(f"[warn] could not fetch Tranco snapshot ({e}); Tranco read-allowlisting "
              f"is inert until you run: python3 setup/fetch_tranco.py")
    return dest


def write_unit(service_name: str, port: int, allowlist: Path,
               python: str, display: str) -> Path:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / f"{service_name}.service"
    unit_path.write_text(UNIT_TEMPLATE.format(
        service_name=service_name, port=port, repo_root=REPO_ROOT,
        python=python, allowlist=allowlist, display=display))
    print(f"[ok]   wrote systemd user unit {unit_path}")
    return unit_path


def systemd_enable(service_name: str) -> None:
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", f"{service_name}.service"]):
        print(f"[run]  {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
    state = subprocess.run(
        ["systemctl", "--user", "is-active", f"{service_name}.service"],
        capture_output=True, text=True).stdout.strip()
    print(f"[ok]   service {service_name} is {state}")
    if state != "active":
        raise SystemExit(
            f"service {service_name} did not become active — inspect with: "
            f"journalctl --user -u {service_name}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"SSE port (default: {DEFAULT_PORT})")
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR,
                        help=f"where the allowlist config lives (default: {DEFAULT_CONFIG_DIR})")
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME,
                        help=f"systemd user service name (default: {DEFAULT_SERVICE_NAME})")
    parser.add_argument("--venv", default=str(DEFAULT_VENV),
                        help=f"venv to create/use for the service (default: {DEFAULT_VENV})")
    parser.add_argument("--python", default=None,
                        help="use an existing interpreter for the service and skip venv "
                             "creation (default: create/use --venv)")
    parser.add_argument("--display", default=os.environ.get("DISPLAY", ":0"),
                        help="DISPLAY for headed Chrome (default: current, else :0)")
    parser.add_argument("--tranco-top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"how many top Tranco domains to fetch (default: {DEFAULT_TOP_N})")
    args = parser.parse_args(argv)

    if not SAMPLE_ALLOWLIST.exists():
        raise SystemExit(f"sample config missing: {SAMPLE_ALLOWLIST} — is the repo intact?")
    if shutil.which("systemctl") is None:
        raise SystemExit(
            "systemctl not found — this script only automates the systemd (Linux) "
            "SSE setup; see the README for manual and stdio instructions.")

    config_dir = Path(args.config_dir).expanduser().resolve()
    allowlist = copy_config(config_dir)
    fetch_tranco(config_dir, args.tranco_top_n)

    if args.python:
        service_python = args.python
        print(f"[ok]   using existing interpreter {service_python} (skipping venv)")
    else:
        service_python = str(ensure_venv(Path(args.venv).expanduser().resolve()))

    write_unit(args.service_name, args.port, allowlist, service_python, args.display)
    systemd_enable(args.service_name)

    agent_json = json.dumps({
        "mcpServers": {
            args.service_name: {
                "type": "sse",
                "url": f"http://127.0.0.1:{args.port}/sse",
            }
        }
    }, indent=2)
    print(
        f"\nDone. Add this to your agent's settings by hand "
        f"(e.g. ~/.claude.json or .gemini/settings.json):\n\n{agent_json}"
    )
    print(
        f"\nThe Tranco top-sites snapshot the read gate uses lives at "
        f"{snapshot_path(config_dir)} (not committed). Refresh it anytime with:\n\n"
        f"    python3 setup/fetch_tranco.py --config-dir {config_dir}\n"
    )
    print(guard_allowlist_note(allowlist))


def guard_allowlist_note(allowlist: Path) -> str:
    """Advisory text nudging the user to gate edits to the allowlist file.

    The allowlist is the security boundary: an agent that can silently rewrite
    it can widen its own read/click permissions. So — at the very least — the
    agent's settings should require approval ("ask") before *every* edit to it.
    Returned as a string (not printed) so it stays easy to test and reuse.
    """
    return (
        f"\nSecurity tip (recommended): {allowlist} is what constrains the "
        f"agent. Keep it from quietly widening its own access by marking edits "
        f"to it as always-ask in your agent's settings — at the very least "
        f'"ask" before every edit, never auto-approve. For Claude Code, add a '
        f'permissions rule like:\n\n    "ask": ["Edit({allowlist})"]\n'
    )


if __name__ == "__main__":
    main()
