"""Runs the real setup/onetime_setup.py end to end under a throwaway service
name and port, so an existing browden service is untouched. Skipped on
hosts without a systemd user session (e.g. some CI runners / macOS)."""
import gzip
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "setup" / "onetime_setup.py"


def _systemd_user_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    r = subprocess.run(["systemctl", "--user", "is-system-running"],
                       capture_output=True, text=True)
    # "running"/"degraded" both mean the user manager is usable.
    return r.stdout.strip() in {"running", "degraded"}


def _run_setup(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(not _systemd_user_available(),
                    reason="no systemd user session on this host")
def test_onetime_setup_installs_config_and_service(tmp_path):
    sys.path.insert(0, str(REPO_ROOT))
    from browden.web_navigator.utils.network_utils import get_free_port

    port = get_free_port()
    service = f"browden-e2e-{port}"
    config_dir = tmp_path / "cfg"
    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{service}.service"
    # Pin the service to this interpreter so setup skips venv creation/install
    # (this test already runs in an env with browden installed).
    args = ["--port", str(port), "--config-dir", str(config_dir),
            "--service-name", service, "--python", sys.executable]

    # Pre-seed the Tranco snapshot so setup's fetch step is skipped — keeps this
    # test offline and fast (a real fetch would download the top-400k list).
    config_dir.mkdir(parents=True, exist_ok=True)
    snapshot = config_dir / "tranco-top-400k.txt.gz"
    with gzip.open(snapshot, "wt", encoding="utf-8") as fh:
        fh.write("google.com\n")

    try:
        res = _run_setup(args)
        assert res.returncode == 0, f"setup failed:\n{res.stdout}\n{res.stderr}"

        # Config copied; unit written; service active.
        assert (config_dir / "allowlist.yaml").read_text() == \
            (REPO_ROOT / "configs" / "samples" / "allowlist.yaml").read_text()
        assert unit_path.exists()
        assert f"--allowlist {config_dir / 'allowlist.yaml'}" in unit_path.read_text()

        # The advertised agent-settings JSON is printed and points at the port.
        assert f"http://127.0.0.1:{port}/sse" in res.stdout
        # The pre-seeded snapshot is left in place (fetch skipped) and the
        # printed output tells the user how to refresh it.
        with gzip.open(snapshot, "rt", encoding="utf-8") as fh:
            assert fh.read() == "google.com\n"
        assert "tranco-top-400k.txt.gz" in res.stdout

        # The SSE server actually comes up on the chosen port.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            log = subprocess.run(
                ["journalctl", "--user", "-u", service, "--no-pager", "-n", "50"],
                capture_output=True, text=True).stdout
            pytest.fail(f"SSE port {port} never opened. Service log:\n{log}")

        # Second run: existing config is preserved, not overwritten.
        marker = "# user edit\n"
        (config_dir / "allowlist.yaml").write_text(
            marker + (config_dir / "allowlist.yaml").read_text())
        res2 = _run_setup(args)
        assert res2.returncode == 0
        assert "already exists" in res2.stdout
        assert (config_dir / "allowlist.yaml").read_text().startswith(marker)
    finally:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{service}.service"],
                       capture_output=True)
        unit_path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
