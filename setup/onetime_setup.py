#!/usr/bin/env python3
"""One-time setup for Browser Guard (SSE transport only; stdio stays manual).

Run it with any Python — `python3 setup/onetime_setup.py`; no venv needed first.

What it does, in order:
  1. Creates a venv at <repo>/.venv and installs browser-guard into it
     (via `uv`, falling back to stdlib venv + pip). Pass --python to use an
     existing interpreter instead and skip this step.
  2. Copies configs/samples/allowlist.yaml to <config-dir>/allowlist.yaml
     (default ~/.browser_guard) — skipped if a config is already there.
  3. Writes a systemd *user* unit that serves SSE on <port>, pinned to that venv.
  4. Runs `systemctl --user daemon-reload` and `enable --now <service-name>`.
  5. Prints the JSON block to add to your agent's settings by hand.

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

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ALLOWLIST = REPO_ROOT / "configs" / "samples" / "allowlist.yaml"
DEFAULT_PORT = 22001  # usually unused; well clear of dev servers on 8000/3000
DEFAULT_CONFIG_DIR = "~/.browser_guard"
DEFAULT_SERVICE_NAME = "browser-guard"
DEFAULT_VENV = REPO_ROOT / ".venv"

UNIT_TEMPLATE = """\
[Unit]
Description=Browser Guard MCP Server (SSE, {service_name})
After=network.target

[Service]
Environment=DISPLAY={display}
Environment=MCP_TRANSPORT=sse
Environment=MCP_HOST=127.0.0.1
Environment=MCP_PORT={port}
WorkingDirectory={repo_root}
ExecStart={python} -m browser_guard.mcp.server --allowlist {allowlist}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _run(cmd: list[str]) -> None:
    print(f"[run]  {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def ensure_venv(venv_dir: Path) -> Path:
    """Create ``venv_dir`` (if absent) and install browser-guard into it editable.

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
    print(f"[ok]   browser-guard installed in {venv_dir}")
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
    args = parser.parse_args(argv)

    if not SAMPLE_ALLOWLIST.exists():
        raise SystemExit(f"sample config missing: {SAMPLE_ALLOWLIST} — is the repo intact?")
    if shutil.which("systemctl") is None:
        raise SystemExit(
            "systemctl not found — this script only automates the systemd (Linux) "
            "SSE setup; see the README for manual and stdio instructions.")

    config_dir = Path(args.config_dir).expanduser().resolve()
    allowlist = copy_config(config_dir)

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
        f"(e.g. ~/.claude.json or .gemini/settings.json):\n\n{agent_json}\n\n"
        f"Edit {allowlist} to change what the agent may read or click "
        f"(then: systemctl --user restart {args.service_name}).\n"
        f"stdio setup remains manual — see the README."
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
