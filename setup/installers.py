"""Per-OS background-service installers for setup/onetime_setup.py.

Each installer renders its native service description (``render()``) and applies
it (``install()``). ``render()`` is pure and side-effect-free so it can be
unit-tested on any OS; ``install()`` touches the real service manager.
``select_installer_cls()`` picks the right one for the host.

Stdlib-only on purpose: this is imported by onetime_setup.py, which runs with any
Python before the venv exists.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

# Service-description templates live as standalone files in setup/templates/ so
# each is editable/reviewable in its native format. Each is a str.format() string
# whose placeholders the installers below fill in at render time.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _template(name: str) -> str:
    """Read a service-description template shipped in setup/templates/."""
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _run(cmd: list[str], check: bool = True,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    print(f"[run]  {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, env=env)


def _pythonw_for(python: str) -> str:
    """The windowless interpreter (pythonw.exe) next to ``python``, if present.

    Used by the Windows service so the background server shows no console.
    Falls back to the given interpreter when pythonw isn't found.
    """
    candidate = Path(python).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else python


class ServiceInstaller:
    """Base for the background-service installers used by --mode service."""

    #: human name of the service manager, for messages
    manager = "service"

    def __init__(self, *, service_name: str, port: int, allowlist: Path,
                 python: str, display: str, repo_root: Path, config_dir: Path):
        self.service_name = service_name
        self.port = port
        self.allowlist = allowlist
        self.python = python
        self.display = display
        self.repo_root = repo_root
        self.config_dir = config_dir

    def render(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def install(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def _require(tool: str) -> None:
        if shutil.which(tool) is None:
            raise SystemExit(
                f"{tool} not found — cannot install the background service on this "
                f"host. Re-run with --mode stdio to let your agent launch the "
                f"server itself (no service manager needed).")


class LinuxSystemdInstaller(ServiceInstaller):
    manager = "systemd"

    def unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{self.service_name}.service"

    def render(self) -> str:
        return _template("systemd.service").format(
            service_name=self.service_name, port=self.port, repo_root=self.repo_root,
            python=self.python, allowlist=self.allowlist, display=self.display)

    def install(self) -> None:
        self._require("systemctl")
        unit_path = self.unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(self.render())
        print(f"[ok]   wrote systemd user unit {unit_path}")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", f"{self.service_name}.service"])
        state = subprocess.run(
            ["systemctl", "--user", "is-active", f"{self.service_name}.service"],
            capture_output=True, text=True).stdout.strip()
        print(f"[ok]   service {self.service_name} is {state}")
        if state != "active":
            raise SystemExit(
                f"service {self.service_name} did not become active — inspect with: "
                f"journalctl --user -u {self.service_name}")


class MacLaunchdInstaller(ServiceInstaller):
    manager = "launchd"

    def plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.service_name}.plist"

    def _log_paths(self) -> tuple[Path, Path]:
        logs = Path.home() / "Library" / "Logs"
        return logs / f"{self.service_name}.log", logs / f"{self.service_name}.err.log"

    def render(self) -> str:
        log, err = self._log_paths()
        return _template("launchd.plist").format(
            service_name=self.service_name, port=self.port, repo_root=self.repo_root,
            python=self.python, allowlist=self.allowlist, log=log, err=err)

    def install(self) -> None:
        self._require("launchctl")
        plist = self.plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        for p in self._log_paths():
            p.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(self.render())
        print(f"[ok]   wrote launchd agent {plist}")
        domain = f"gui/{os.getuid()}"
        # Idempotent: unload a previous instance (ignore failure), then load.
        _run(["launchctl", "bootout", domain, str(plist)], check=False)
        _run(["launchctl", "bootstrap", domain, str(plist)])
        _run(["launchctl", "kickstart", "-k", f"{domain}/{self.service_name}"], check=False)
        state = subprocess.run(
            ["launchctl", "print", f"{domain}/{self.service_name}"],
            capture_output=True, text=True)
        active = state.returncode == 0
        print(f"[ok]   service {self.service_name} is {'active' if active else 'not running'}")
        if not active:
            raise SystemExit(
                f"launchd agent {self.service_name} did not load — inspect with: "
                f"launchctl print {domain}/{self.service_name}")


class WindowsTaskInstaller(ServiceInstaller):
    manager = "Task Scheduler"

    def launcher_path(self) -> Path:
        return self.config_dir / f"{self.service_name}-service.pyw"

    def render_launcher(self) -> str:
        return _template("service-launcher.py").format(
            port=self.port, allowlist=self.allowlist)

    def render(self) -> str:
        # Every placeholder lands in XML *element text*, so XML-escape each value:
        # a path or service name containing & < > (e.g. C:\dev\a&b\…) would
        # otherwise produce malformed XML that Task Scheduler refuses to import.
        return _template("windows-task.xml").format(
            service_name=_xml_escape(self.service_name),
            repo_root=_xml_escape(str(self.repo_root)),
            pythonw=_xml_escape(_pythonw_for(self.python)),
            launcher=_xml_escape(str(self.launcher_path())))

    def install(self) -> None:
        self._require("schtasks")
        launcher = self.launcher_path()
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(self.render_launcher())
        print(f"[ok]   wrote service launcher {launcher}")
        xml_path = self.config_dir / f"{self.service_name}-task.xml"
        xml_path.write_text(self.render(), encoding="utf-16")
        print(f"[ok]   wrote task definition {xml_path}")
        _run(["schtasks", "/create", "/tn", self.service_name,
              "/xml", str(xml_path), "/f"])
        _run(["schtasks", "/run", "/tn", self.service_name], check=False)
        print(f"[ok]   scheduled task {self.service_name} created and started")


def select_installer_cls(platform: str = sys.platform, os_name: str = os.name):
    """Pick the service installer for the host (overridable for tests)."""
    if platform == "darwin":
        return MacLaunchdInstaller
    if os_name == "nt":
        return WindowsTaskInstaller
    return LinuxSystemdInstaller
