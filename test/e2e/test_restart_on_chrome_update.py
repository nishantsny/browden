"""End-to-end: a reaper tick really replaces a Chrome that was updated under it.

The unit tests pin the decisions with Chrome faked at its boundaries: which
versions count as an update, that the tick checks only while the key is on, and
what is forgotten. What they cannot show is the real chain an operator depends
on: a running Chrome reports its version through the attached driver, the binary
on disk reports another, the tick stops the real process, and the next tool call
gets a working browser again.

An upgrade is staged without installing anything. ``BROWDEN_CHROME_BINARY``
points at a wrapper script that runs the real Chrome for every launch but
answers ``--version`` from a file the test rewrites. Writing the real version
there is "nothing installed since launch"; writing another one is "apt replaced
the binary". The wrapper ``exec``s Chrome, so the process the backend tracks is
Chrome itself and its exit is the real browser stopping.
"""
import asyncio
import os
import stat

import pytest

from browden.mcp.session_management.browser_session_manager import BrowserSessionManager
from browden.web_navigator.selenium_chrome import backend as backend_mod

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the version wrapper is a POSIX shell script")

REAP_INTERVAL = 0.2
PATIENCE_SECONDS = 15.0


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """The version the "installed" Chrome reports; ``installed.set(v)`` changes it.

    Starts out as the real binary's own version, so the browser the test launches
    matches what is on disk until the test stages an update.
    """
    real_binary = backend_mod._find_chrome_binary()
    real_version = backend_mod._installed_chrome_version()
    assert real_version, f"could not read the version of {real_binary}"

    version_file = tmp_path / "installed-version"
    wrapper = tmp_path / "chrome"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "Google Chrome $(cat {version_file})"; exit 0; fi\n'
        f'exec "{real_binary}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("BROWDEN_CHROME_BINARY", str(wrapper))

    class Installed:
        real = real_version

        def set(self, version: str) -> None:
            version_file.write_text(version)

    result = Installed()
    result.set(real_version)
    return result


@pytest.fixture
def make_manager(new_backend, tmp_path):
    """``make(auto_restart) -> (manager, backend)`` with a fast live reaper; cancelled after."""
    made: list[BrowserSessionManager] = []

    def make(auto_restart: bool):
        backend = new_backend(tmp_path / "profile")
        manager = BrowserSessionManager(
            backend, namespace="e2e",
            reap_interval_seconds=lambda: REAP_INTERVAL,
            auto_restart_on_chrome_update=lambda: auto_restart)
        made.append(manager)
        return manager, backend

    yield make

    for manager in made:
        if manager._reaper_task is not None:
            manager._reaper_task.cancel()


async def _wait_until(predicate, message: str) -> None:
    deadline = asyncio.get_running_loop().time() + PATIENCE_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out after {PATIENCE_SECONDS:g}s waiting for: {message}")


@pytest.mark.asyncio
async def test_tick_restarts_chrome_only_once_the_installed_version_changes(installed, make_manager):
    manager, backend = make_manager(auto_restart=True)
    tab = await manager.new_blank_tab(max_tabs=10)
    first = backend._chrome_proc
    assert backend._driver.capabilities["browserVersion"] == installed.real

    # Same version on disk: several ticks pass and the browser is left alone.
    await asyncio.sleep(REAP_INTERVAL * 5)
    assert first.poll() is None
    assert backend._chrome_proc is first

    # "apt upgraded Chrome": the next tick stops the running browser and
    # forgets the tabs that lived in it.
    installed.set("999.0.0.1")
    # Both, not just the exit: the process is gone while the teardown is still
    # running off the loop, before the tick gets back to dropping the tabs.
    await _wait_until(lambda: first.poll() is not None and not manager._registry.tracked_ids(),
                      "the tick to stop the outdated Chrome and forget its tabs")

    # The upgrade "lands" (any relaunch from here runs a binary matching what is
    # on disk, so no later tick stops it again), and the next tool calls get a
    # working browser, in which the old tab's id names nothing.
    installed.set(installed.real)
    assert (await manager.select_tab(tab["id"]))["error"]
    fresh = await manager.new_blank_tab(max_tabs=10)
    assert fresh["id"] != tab["id"]
    assert backend._chrome_proc is not first and backend._chrome_proc.poll() is None


@pytest.mark.asyncio
async def test_tick_leaves_an_outdated_chrome_running_when_the_key_is_off(installed, make_manager):
    manager, backend = make_manager(auto_restart=False)
    await manager.new_blank_tab(max_tabs=10)
    first = backend._chrome_proc

    installed.set("999.0.0.1")
    await asyncio.sleep(REAP_INTERVAL * 5)  # ample ticks had the key been on

    assert first.poll() is None
    assert backend._chrome_proc is first
