"""E2e: the software-WebGL switch against a real Chrome.

The unit tests pin the decisions — which argv the next launch gets, when a
relaunch happens, what is forgotten. What they cannot show is that the flag
reaches the browser that is actually running and that the session survives being
replaced underneath it, which is what this checks: Chrome's own command line is
read back from ``/proc``, so the assertion is about the process on this host
rather than about what the code intended to pass it.

Deliberately *not* asserted here: that the flag makes ``getContext('webgl')``
work. It does on a GPU-less headful host (measured on the deployment box), but
headless Chrome rasterises WebGL in software whether or not the flag is given,
so an assertion on WebGL availability would pass for the wrong reason in CI.
"""
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.web_navigator.utils.network_utils import get_free_port
from gates import OPEN_READ_GATE

sys.path.insert(0, os.path.dirname(__file__))

FLAG = "--enable-unsafe-swiftshader"


class _Handler(BaseHTTPRequestHandler):
    """Reports whether this browser gave the page a WebGL context."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"""<html><body><div id='state'>pending</div><script>
          const gl = document.createElement('canvas').getContext('webgl');
          document.getElementById('state').textContent = gl ? 'webgl' : 'none';
        </script></body></html>""")

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    port = get_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join()


@pytest.fixture
def profile(tmp_path):
    return tmp_path / "profile"


@pytest.fixture
def session(new_backend, profile):
    return BrowserSessionManager(new_backend(profile), namespace="e2e", start_reaper=False)


def _browser_command_line(profile_dir):
    """The live browser process's command line for ``profile_dir``, read from /proc.

    An outside observation on purpose: it says what the browser on this host was
    actually started with, not what the backend believes it passed.

    Two quirks are handled here. Chrome rewrites its own ``/proc`` cmdline as a
    single space-joined blob rather than NUL-separated argv, so this matches on
    the text. And only the browser process is wanted: its renderers, zygotes and
    utility processes share the profile but are given their own flag sets, so
    they are excluded by the ``--type=`` every one of them carries.
    """
    want = re.escape(f"--user-data-dir={profile_dir}")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            line = (entry / "cmdline").read_bytes().decode(errors="replace").replace("\0", " ")
        except OSError:
            continue          # the process exited while we were looking
        if (re.search(rf"{want}(\s|$)", line)
                and "--remote-debugging-port=" in line and "--type=" not in line):
            return line
    return None


def _launched_with_flag(profile_dir):
    line = _browser_command_line(profile_dir)
    assert line, f"no browser process found for {profile_dir}"
    return FLAG in line


async def _open(session, **kwargs):
    return await session.new_blank_tab(max_tabs=10, **kwargs)


@pytest.mark.asyncio
async def test_the_default_browser_runs_without_the_flag(session, profile):
    tab = await _open(session)

    assert tab["software_webgl"] == 0
    assert tab["relaunched"] is False
    assert not _launched_with_flag(profile)


@pytest.mark.asyncio
async def test_asking_for_software_webgl_relaunches_chrome_with_the_flag(session, profile):
    await _open(session)
    assert not _launched_with_flag(profile)

    tab = await _open(session, software_webgl=True)

    assert tab["software_webgl"] == 1
    assert tab["relaunched"] is True
    assert _launched_with_flag(profile)


@pytest.mark.asyncio
async def test_the_relaunched_browser_still_works(session, site):
    await _open(session, software_webgl=True)
    tab = await _open(session, software_webgl=True)

    # A tab in the replaced browser navigates and reads like any other: the
    # switch must not leave the session half-attached.
    await session.navigate(f"{site}/page", id=tab["id"], gate=OPEN_READ_GATE)
    state = await session.query_selector("#state", id=tab["id"], gate=OPEN_READ_GATE)

    assert state["found"] is True
    assert state["element"]["text"] in ("webgl", "none")


@pytest.mark.asyncio
async def test_the_old_tabs_id_dies_with_the_browser_it_belonged_to(session, site):
    doomed = await _open(session)
    await session.navigate(f"{site}/page", id=doomed["id"], gate=OPEN_READ_GATE)

    await _open(session, software_webgl=True)

    # Honest failure rather than a silent answer from some other tab.
    result = await session.navigate(f"{site}/page", id=doomed["id"], gate=OPEN_READ_GATE)
    assert "error" in result
    assert result["id"] == doomed["id"]


@pytest.mark.asyncio
async def test_asking_for_the_mode_already_in_force_keeps_the_tabs(session, site):
    await _open(session, software_webgl=True)
    keeper = await _open(session, software_webgl=True)

    second = await _open(session, software_webgl=True)

    assert second["relaunched"] is False
    # The earlier tab is still live and drivable, so a caller may pass its
    # preference on every tab without losing work.
    result = await session.navigate(f"{site}/page", id=keeper["id"], gate=OPEN_READ_GATE)
    assert result["id"] == keeper["id"]


@pytest.mark.asyncio
async def test_turning_it_off_again_relaunches_without_the_flag(session, profile):
    await _open(session, software_webgl=True)
    assert _launched_with_flag(profile)

    tab = await _open(session, software_webgl=False)

    assert tab["software_webgl"] == 0
    assert tab["relaunched"] is True
    assert not _launched_with_flag(profile)


@pytest.mark.asyncio
async def test_omitting_it_leaves_the_browser_alone(session, profile, site):
    await _open(session, software_webgl=True)
    keeper = await _open(session, software_webgl=True)

    plain = await _open(session)

    # No argument means no opinion: same browser, same tabs, mode untouched.
    assert plain["relaunched"] is False
    assert plain["software_webgl"] == 1
    assert _launched_with_flag(profile)
    result = await session.navigate(f"{site}/page", id=keeper["id"], gate=OPEN_READ_GATE)
    assert result["id"] == keeper["id"]
