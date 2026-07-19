"""Runs the real setup/onetime_setup.py end to end under a throwaway service
name and port, so an existing browden service is untouched. Skipped on
hosts without a systemd user session (e.g. some CI runners / macOS)."""
import gzip
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Opening sentinel of the auto-written provenance block (setup/checkpoints.py).
# Everything above it is a verbatim copy of the sample; the block itself carries
# per-install snapshot checksums and so diverges from the committed placeholders.
_PROVENANCE_MARKER = "# >>> browden provenance"
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
    args = ["--mode", "service", "--port", str(port), "--config-dir", str(config_dir),
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

        # Config copied; unit written; service active. The body above the
        # auto-written provenance block matches the sample byte-for-byte — setup
        # only rewrites the block itself, filling in checksums for the snapshots
        # it fetched (asserted just below), so don't compare that tail.
        installed = (config_dir / "allowlist.yaml").read_text()
        sample = (REPO_ROOT / "configs" / "samples"
                  / "read_only_on_popular_websites.yaml").read_text()
        assert installed.split(_PROVENANCE_MARKER, 1)[0] == \
            sample.split(_PROVENANCE_MARKER, 1)[0]

        # setup fetched the PSL (its snapshot wasn't pre-seeded) and recorded its
        # checksum in the provenance block; the Tranco snapshot WAS pre-seeded so
        # its fetch was skipped and those fields stay empty placeholders.
        provenance = _PROVENANCE_MARKER + installed.split(_PROVENANCE_MARKER, 1)[1]
        assert re.search(r"^#\s*pal_checksum_sha256: [0-9a-f]{64}$",
                         provenance, re.MULTILINE)
        assert re.search(r"^#\s*tranco_checksum_sha256:\s*$",
                         provenance, re.MULTILINE)

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


def test_onetime_setup_stdio_mode_prints_config_and_writes_no_service(tmp_path):
    """The default (stdio) mode does the venv/config/Tranco work and prints an
    stdio config block — no systemd unit, no SSE port. Needs no systemd session."""
    service = "browden-stdio-test"
    config_dir = tmp_path / "cfg"
    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{service}.service"
    assert not unit_path.exists(), "unexpected leftover unit from a prior run"

    # Pre-seed Tranco (offline/fast) and pin --python so no venv is built.
    config_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(config_dir / "tranco-top-400k.txt.gz", "wt", encoding="utf-8") as fh:
        fh.write("google.com\n")

    # --mode stdio is the default, but pass it explicitly to pin the contract.
    res = _run_setup(["--mode", "stdio", "--config-dir", str(config_dir),
                      "--service-name", service, "--python", sys.executable])
    assert res.returncode == 0, f"setup failed:\n{res.stdout}\n{res.stderr}"

    # Config copied, but no service written and no SSE URL advertised.
    assert (config_dir / "allowlist.yaml").exists()
    assert not unit_path.exists()
    assert "/sse" not in res.stdout
    # The printed block is a stdio launcher for this interpreter.
    assert '"command"' in res.stdout
    assert "browden.mcp.server" in res.stdout
    assert str(config_dir / "allowlist.yaml") in res.stdout
