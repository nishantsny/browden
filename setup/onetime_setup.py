#!/usr/bin/env python3
"""One-time setup for Browser Guard (SSE transport only; stdio stays manual).

What it does, in order:
  1. Copies configs/samples/allowlist.yaml to <config-dir>/allowlist.yaml
     (default ~/.browser_guard) — skipped if a config is already there.
  2. Writes a systemd *user* unit that serves SSE on <port> with the Python
     interpreter running this script (activate your venv first).
  3. Runs `systemctl --user daemon-reload` and `enable --now <service-name>`.
  4. Prints the JSON block to add to your agent's settings by hand.

Run it again anytime: the config copy is skipped when present and the systemd
steps are idempotent. Use --service-name/--port to stand up a second instance
without disturbing an existing one.
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
    parser.add_argument("--python", default=sys.executable,
                        help="Python the service runs (default: the one running this script)")
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
    write_unit(args.service_name, args.port, allowlist, args.python, args.display)
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


if __name__ == "__main__":
    main()
