"""Unit tests for setup/onetime_setup.py — the pure, cross-platform pieces
(venv layout, installer selection, rendered service definitions, config blocks).
No service manager is touched; the real end-to-end systemd path is covered by
test/e2e/test_setup_script.py."""
import json
import sys
from pathlib import Path

import pytest

SETUP_DIR = Path(__file__).resolve().parents[3] / "setup"
sys.path.insert(0, str(SETUP_DIR))

import onetime_setup as ots  # noqa: E402


# -- venv interpreter layout -------------------------------------------------

def test_venv_python_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(ots.os, "name", "posix")
    assert ots._venv_python(tmp_path) == tmp_path / "bin" / "python"


def test_venv_python_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(ots.os, "name", "nt")
    assert ots._venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"


def test_pythonw_for_prefers_windowless_sibling(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert ots._pythonw_for(str(tmp_path / "python.exe")) == str(tmp_path / "pythonw.exe")


def test_pythonw_for_falls_back_when_absent(tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("")
    assert ots._pythonw_for(str(python)) == str(python)


# -- installer selection -----------------------------------------------------

def test_select_installer_cls_per_platform():
    assert ots.select_installer_cls("linux", "posix") is ots.LinuxSystemdInstaller
    assert ots.select_installer_cls("darwin", "posix") is ots.MacLaunchdInstaller
    assert ots.select_installer_cls("win32", "nt") is ots.WindowsTaskInstaller


def _installer(cls, tmp_path):
    return cls(
        service_name="browden-test", port=22050,
        allowlist=tmp_path / "allowlist.yaml", python="/venv/bin/python",
        display=":0", repo_root=tmp_path / "repo", config_dir=tmp_path / "cfg")


# -- rendered service definitions --------------------------------------------

def test_systemd_unit_render(tmp_path):
    unit = _installer(ots.LinuxSystemdInstaller, tmp_path).render()
    assert "Environment=MCP_PORT=22050" in unit
    assert "Environment=MCP_TRANSPORT=sse" in unit
    assert "-m browden.mcp.server --allowlist" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit


def test_launchd_plist_render(tmp_path):
    plist = _installer(ots.MacLaunchdInstaller, tmp_path).render()
    assert "<key>Label</key><string>browden-test</string>" in plist
    assert "<key>KeepAlive</key><true/>" in plist       # ~ Restart=always
    assert "<key>RunAtLoad</key><true/>" in plist        # ~ start at login
    assert "<key>MCP_PORT</key><string>22050</string>" in plist
    assert "browden.mcp.server" in plist


def test_windows_task_render(tmp_path):
    inst = _installer(ots.WindowsTaskInstaller, tmp_path)
    task = inst.render()
    assert "<LogonTrigger>" in task                       # ~ start at login
    assert "<RestartOnFailure>" in task                    # ~ Restart=always
    assert str(inst.launcher_path()) in task
    assert inst.launcher_path().suffix == ".pyw"
    launcher = inst.render_launcher()
    assert 'os.environ.setdefault("MCP_PORT", "22050")' in launcher
    assert "browden.mcp.server" in launcher
    assert "allowlist.yaml" in launcher


# -- agent config blocks -----------------------------------------------------

def test_sse_config_points_at_port():
    cfg = json.loads(ots.sse_config("browden", 22050))
    entry = cfg["mcpServers"]["browden"]
    assert entry == {"type": "sse", "url": "http://127.0.0.1:22050/sse"}


def test_stdio_config_includes_display_on_linux(tmp_path):
    allow = tmp_path / "allowlist.yaml"
    cfg = json.loads(ots.stdio_config("browden", "/venv/bin/python", allow, ":0"))
    entry = cfg["mcpServers"]["browden"]
    assert entry["command"] == "/venv/bin/python"
    assert entry["args"] == ["-m", "browden.mcp.server", "--allowlist", str(allow)]
    assert entry["env"] == {"DISPLAY": ":0"}


def test_stdio_config_omits_env_without_display(tmp_path):
    cfg = json.loads(ots.stdio_config("browden", "py", tmp_path / "a.yaml", None))
    assert "env" not in cfg["mcpServers"]["browden"]
