import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

from ...common.logger import logger
from ...common.tab import TabInfo
from ...dependencies.selenium import (
    By,
    ChromeOptions,
    InvalidElementStateException,
    Keys,
    NoSuchElementException,
    NoSuchWindowException,
    WebDriverWait,
    webdriver,
)
from ..interface import TabNotFoundError, WebNavigatorBackend
from ..utils.network_utils import get_free_port


def _switch(drv, handle: str) -> None:
    """Focus a tab by id, translating Selenium's missing-window error.

    Selenium's NoSuchWindowException stringifies to a multi-line driver stack
    trace; TabNotFoundError carries a clean, actionable message instead.
    """
    try:
        drv.switch_to.window(handle)
    except NoSuchWindowException:
        raise TabNotFoundError(f"tab {handle!r} is not open") from None


def _headless_enabled() -> bool:
    """Whether to launch Chrome headless, controlled by ``BROWDEN_HEADLESS``.

    A real human-facing session wants a visible window, so this defaults to off.
    Set ``BROWDEN_HEADLESS=1`` (or true/yes/on) for environments without a
    display — e2e tests and CI runners.
    """
    return os.environ.get("BROWDEN_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")


def _no_sandbox_enabled() -> bool:
    """Whether to disable Chrome's setuid/namespace sandbox — opt-in only.

    ``--no-sandbox`` removes a layer of renderer isolation, exactly the layer
    that matters most on the hosts browden is likely to drive at hostile web
    content (CI runners, containers, servers). So we default to keeping the
    sandbox ON and only drop it when the operator explicitly sets
    ``BROWDEN_NO_SANDBOX=1`` (or true/yes/on) — the escape hatch for environments
    where the sandbox cannot start, e.g. an unprivileged container or a runner
    with user namespaces disabled, where Chrome would otherwise refuse to launch.
    """
    return os.environ.get("BROWDEN_NO_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")


SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
TITLE_WAIT_SECONDS = 3
# Cold Chrome starts can take several seconds; under concurrent launches (many
# profiles at once) plus a loaded host, give the DevTools endpoint generous
# margin before declaring the launch failed.
DEVTOOLS_WAIT_SECONDS = 60
CHROME_BINARY_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


def _wellknown_chrome_paths(platform: str = sys.platform, os_name: str = os.name) -> list[str]:
    """OS-specific install locations to try when Chrome isn't on PATH.

    On macOS and Windows, Chrome installs to a fixed application directory that
    is *not* on ``PATH`` and whose executable isn't named ``google-chrome`` — so
    ``shutil.which`` never finds it. Check those canonical spots before giving
    up. Linux keeps to PATH (packaged installs land there), so this is empty.

    ``platform``/``os_name`` are injectable so the per-OS branches are testable
    from any host without perturbing ``os.name`` (which flips pathlib's flavour).
    """
    home = Path.home()
    if platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    if os_name == "nt":
        bases = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")),
        ]
        return [str(Path(b) / "Google" / "Chrome" / "Application" / "chrome.exe")
                for b in bases if b]
    return []


def _find_chrome_binary() -> str:
    """Locate the Chrome/Chromium executable to launch directly.

    Honours ``BROWDEN_CHROME_BINARY`` (or the common ``CHROME_BIN``) first, then
    the usual binary names on PATH, then each platform's canonical install
    location (macOS ``/Applications``, Windows ``Program Files``) — so a stock
    Chrome install works without setting anything.
    """
    explicit = os.environ.get("BROWDEN_CHROME_BINARY") or os.environ.get("CHROME_BIN")
    if explicit:
        return explicit
    for name in CHROME_BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in _wellknown_chrome_paths():
        if Path(path).exists():
            return path
    raise RuntimeError(
        "Could not find a Chrome/Chromium binary on PATH or in the usual install "
        "locations; set BROWDEN_CHROME_BINARY to its full path."
    )


def _free_port() -> int:
    """Reserve an ephemeral localhost port for Chrome's DevTools endpoint."""
    return get_free_port()


def _chrome_args(profile_dir: Path, port: int) -> list[str]:
    """Build the command line for a human-looking Chrome.

    Deliberately omits every automation switch chromedriver would otherwise
    inject when *it* launches Chrome (``--enable-automation``, the
    ``AutomationControlled`` blink feature, ``--test-type``, …). Because we
    start Chrome ourselves and Selenium only attaches over the DevTools port,
    ``navigator.webdriver`` stays false and there is no "controlled by
    automated test software" infobar — the window passes for an ordinary,
    human-driven browser. Only benign flags a real launcher commonly sets are
    included here.
    """
    args = [
        _find_chrome_binary(),
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        # Chrome >= 111 rejects DevTools websocket connections from a foreign
        # origin. Scope allowed origins to the exact loopback origin Selenium
        # connects from (debuggerAddress is 127.0.0.1:<port>) rather than "*":
        # a wildcard lets any web page that reaches this ephemeral loopback
        # port — e.g. a malicious page scanning loopback ports, whose Origin
        # header the browser sets and the page cannot forge — open a DevTools
        # websocket and take full CDP control (read every cookie, run JS in any
        # origin), bypassing every browden gate. Pinning the origin keeps
        # Selenium working while shutting that out. (A hostile *native* local
        # process can spoof any Origin header, so this flag is no defence
        # there — local processes are trusted by design, see SECURITY.md.)
        f"--remote-allow-origins=http://127.0.0.1:{port}",
        "--no-first-run",
        "--no-default-browser-check",
        # Guarantee a WebGL context even on GPU-less hosts (VMs, containers, our
        # Hyper-V deployment box: hyperv_drm exposes no DRM render node, so
        # Chrome's ANGLE/EGL backend can't initialise hardware GL). Chrome >= 121
        # no longer falls back to its bundled SwiftShader for WebGL by default,
        # so getContext('webgl') returns null — a browser advertising *zero*
        # WebGL support is a decisive bot signal for Akamai & friends, which
        # then 403 the site's order-data XHRs (see issue #29). Re-enabling the
        # software fallback yields a real context (ANGLE/SwiftShader renderer)
        # and clears that flag. On a host with a real GPU this is inert — Chrome
        # keeps using hardware GL and only reaches for SwiftShader as a fallback.
        "--enable-unsafe-swiftshader",
    ]
    if _headless_enabled():
        # New headless mode + the flags a headless container commonly needs.
        logger.info("Launching Chrome headless")
        args += [
            "--headless=new",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1280,1024",
        ]
    if _no_sandbox_enabled():
        # Opt-in only (BROWDEN_NO_SANDBOX): dropping the sandbox weakens renderer
        # isolation, so we never do it implicitly — not even headless. Set it on
        # hosts where the sandbox can't start (unprivileged containers, runners
        # with user namespaces disabled).
        logger.warning("Launching Chrome with --no-sandbox (renderer sandbox disabled)")
        args.append("--no-sandbox")
    return args


def _wait_for_devtools(port: int, proc: subprocess.Popen, timeout: float = DEVTOOLS_WAIT_SECONDS) -> None:
    """Block until Chrome's DevTools HTTP endpoint answers, or fail loudly.

    Bails early (rather than waiting out the timeout) if the Chrome process
    exits before the port comes up.
    """
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Chrome exited (code {proc.returncode}) before its DevTools "
                f"endpoint came up on port {port}."
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"Chrome DevTools endpoint not ready on port {port} after {timeout}s.")


def _terminate(proc: subprocess.Popen | None) -> None:
    """Stop a Chrome subprocess, escalating to kill if it lingers."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        logger.warning(f"Failed to terminate Chrome process: {e}")


def _launch_chrome(profile_dir: Path) -> tuple[subprocess.Popen, int]:
    """Launch Chrome directly (no chromedriver) with remote debugging on.

    Returns the running process and the DevTools port to attach Selenium to.
    """
    _clear_stale_singletons(profile_dir)
    port = _free_port()
    args = _chrome_args(profile_dir, port)
    logger.info(f"Launching Chrome directly: {' '.join(args)}")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_for_devtools(port, proc)
    except Exception:
        _terminate(proc)
        raise
    return proc, port


def _wait_for_title(drv, timeout: float = TITLE_WAIT_SECONDS) -> None:
    """Wait briefly for the page title to populate after navigation.

    drv.get() returns when the load event fires, but many tabs set their
    final title via JavaScript after that. Best-effort: don't raise if the
    title never appears.
    """
    try:
        WebDriverWait(drv, timeout).until(lambda d: bool(d.title))
    except Exception:
        pass


def _clear_stale_singletons(profile_dir: Path) -> None:
    """Remove Singleton* files left over from an unclean shutdown.

    Chrome creates these symlinks when it starts and removes them on clean
    exit. After a power-off, kill -9, or container restart they point at
    dead PIDs and block the next launch.
    """
    for name in SINGLETON_FILES:
        path = profile_dir / name
        try:
            if path.exists():
                logger.info(f"Clearing stale singleton: {path}")
                path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Failed to clear stale singleton {path}: {e}")


class SeleniumChromeBackend(WebNavigatorBackend):
    """Selenium WebDriver implementation of the navigator backend.

    Each instance owns exactly one Chrome session bound to one profile
    directory (``--user-data-dir``). ``profile_dir`` is required — the caller
    picks the path (the MCP server resolves the shared default and hands it in;
    the backend never falls back to a default of its own); distinct profiles
    don't share the per-dir ``SingletonLock``, so two backends on two profiles
    run concurrently without clobbering each other's window focus.

    Tab handles are raw Selenium window handles: the backend has no notion of the
    server's composite ``<profile>-<handle>`` id (see ``WebNavigatorBackend``).
    """

    def __init__(self, profile_dir):
        # profile_dir is required and immutable: it binds this backend to one
        # Chrome --user-data-dir (the caller resolves the shared default path;
        # the backend never falls back to a module default). Exposed read-only
        # via get_profile_dir().
        if not profile_dir:
            raise ValueError("profile_dir is required")
        self._driver = None
        self._chrome_proc = None
        self._profile_dir = Path(profile_dir).expanduser()
        # Per-tab iframe focus. handle -> ordered list of iframe CSS selectors
        # (the path from the top document to the currently-focused frame). Empty /
        # absent means the tab is focused on its top document. switch_to.window
        # (select_tab) resets Chrome's frame context to top on every focus, so this
        # path is REPLAYED after each focus (_replay_frames) to keep a tab "inside"
        # its frame across the window-focus that nearly every op performs.
        self._frame_paths: dict[str, list[str]] = {}
        self._focused: str | None = None  # the handle select_tab last focused

    def get_profile_dir(self) -> Path:
        return self._profile_dir

    def shutdown(self) -> None:
        """Public teardown for orderly shutdown (see ``WebNavigatorBackend.shutdown``).

        Delegates to the same ``_teardown`` used internally to heal a dead
        session, so a stopped backend leaves no orphaned Chrome or stale
        SingletonLock behind.
        """
        self._teardown()

    def _teardown(self) -> None:
        """Drop the WebDriver session and stop the Chrome we launched.

        Selenium only *attached* to Chrome over the DevTools port, so
        ``driver.quit()`` detaches without closing the browser — we own the
        process and must terminate it ourselves, or it (and its SingletonLock)
        outlives the dead session and blocks the next launch.
        """
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        _terminate(self._chrome_proc)
        self._chrome_proc = None

    def _session_alive(self, check_current: bool) -> bool:
        """Whether the attached driver is live; tears it down (no relaunch) on failure.

        The single liveness probe both ``is_running`` and ``_drv`` share. Always
        checks ``window_handles`` (the session is up); with ``check_current`` also
        checks ``current_window_handle`` (the focused-window context later commands
        need — a "no such window" here is what would otherwise blow up mid-op).
        A failing probe tears the dead session down and returns False, so callers
        decide what next without a browser being spawned here: ``is_running``
        reports it, ``_drv`` relaunches.
        """
        if self._driver is None:
            return False
        try:
            _ = self._driver.window_handles
            if check_current:
                _ = self._driver.current_window_handle
            return True
        except Exception:
            self._teardown()
            return False

    def is_running(self) -> bool:
        """True if a live Chrome is currently attached. Probes without launching.

        The observing counterpart to ``_drv()``'s heal-by-relaunch: callers that
        only want to look (list_tabs across profiles) must not spawn a browser
        as a side effect. Only the session-level probe — a valid *current window*
        is ``_drv``'s concern (it can heal one), not this look-only check.
        """
        return self._session_alive(False)

    def _drv(self):
        # Health-check the attached driver (session AND current-window context);
        # a failed probe has already torn it down, so (re)launch a fresh one.
        if not self._session_alive(True):
            profile = self._profile_dir
            logger.info(f"Starting new Chrome session (profile={profile})")
            self._chrome_proc, port = _launch_chrome(profile)
            # Attach to the Chrome we just launched instead of letting
            # chromedriver spawn its own (automation-flagged) instance.
            opts = ChromeOptions()
            opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
            try:
                self._driver = webdriver.Chrome(options=opts)
            except Exception:
                self._teardown()
                raise
        return self._driver

    def _tabinfo(self, drv, *, selected: bool) -> TabInfo:
        """Snapshot the driver's *currently focused* window as a TabInfo.

        The four TabInfo constructions (list_tabs / new_blank_tab / navigate /
        reload) all read the same fields off the focused window and only differ in
        ``selected`` — so callers switch to the window they mean, then ask here.
        """
        return TabInfo(
            handle=drv.current_window_handle,
            url=drv.current_url,
            title=drv.title,
            selected=selected,
            profile_dir=str(self._profile_dir),
        )

    @staticmethod
    def _resolve_one_visible(drv, css_selector: str):
        """Re-find a policy-validated selector live and return its single actionable element.

        The identical resolve-and-vet block ``click_element`` and
        ``insert_text_element`` both run. Ambiguity is a deny: the policy layer
        validated exactly one element on the snapshot, so anything other than one
        live match — or a match that is hidden or disabled — means the DOM moved
        under us, and the caller must not act on it.
        """
        matches = drv.find_elements(By.CSS_SELECTOR, css_selector)
        if len(matches) == 0:
            raise NoSuchElementException(f"no element matches {css_selector!r}")
        if len(matches) > 1:
            raise ValueError(f"selector {css_selector!r} matched {len(matches)} live elements")
        el = matches[0]
        if not el.is_displayed():
            raise ValueError("target element is not visible")
        if not el.is_enabled():
            raise ValueError("target element is disabled")
        return el

    def list_tabs(self) -> list[TabInfo]:
        drv = self._drv()
        current_handle = drv.current_window_handle
        tabs = []
        for handle in drv.window_handles:
            drv.switch_to.window(handle)
            tabs.append(self._tabinfo(drv, selected=(handle == current_handle)))
        drv.switch_to.window(current_handle)
        return tabs

    def list_handles(self) -> list[str]:
        return list(self._drv().window_handles)

    def new_blank_tab(self) -> TabInfo:
        drv = self._drv()
        drv.switch_to.new_window("tab")
        return self._tabinfo(drv, selected=True)

    def close_tab(self, handle: str) -> None:
        drv = self._drv()
        if len(drv.window_handles) == 1:
            raise ValueError("Cannot close the last tab")
        _switch(drv, handle)
        drv.close()
        self._frame_paths.pop(handle, None)  # forget the closed tab's frame focus
        # drv.close() leaves the driver focused on the now-dead handle. The next
        # command — or _drv()'s health check, which reads current_window_handle —
        # would then mistake the session for dead and relaunch the whole browser,
        # taking every other tab with it. Re-focus a survivor to keep the session
        # valid.
        remaining = drv.window_handles
        if remaining:
            drv.switch_to.window(remaining[0])

    def select_tab(self, handle: str) -> None:
        _switch(self._drv(), handle)
        # switch_to.window just reset the frame context to this window's top
        # document; restore whatever frame this tab was focused into.
        self._focused = handle
        self._replay_frames(handle)

    def _replay_frames(self, handle: str) -> None:
        """Re-enter the frame path recorded for ``handle`` after a window focus.

        ``switch_to.window`` (in ``select_tab``) always lands on the top document,
        so a tab that the agent switched into an iframe must be walked back down its
        recorded selector path before any DOM op runs — otherwise the soup cache
        would read the *top* page's ``page_source`` and the agent would silently
        lose the frame. If any hop no longer resolves to exactly one frame (the page
        changed under us), we forget the path and stay at the top document rather
        than act in the wrong context.
        """
        path = self._frame_paths.get(handle)
        if not path:
            return
        drv = self._drv()
        drv.switch_to.default_content()
        try:
            for selector in path:
                matches = drv.find_elements(By.CSS_SELECTOR, selector)
                if len(matches) != 1:
                    raise NoSuchElementException(f"frame path hop {selector!r} no longer resolves")
                drv.switch_to.frame(matches[0])
        except Exception as e:
            logger.info(f"Frame path for tab {handle} no longer valid ({e}); reset to top document")
            drv.switch_to.default_content()
            self._frame_paths[handle] = []

    def navigate(self, url: str) -> TabInfo:
        drv = self._drv()
        self._reset_frames(drv)  # a new top document — drop any recorded frame focus
        try:
            drv.get(url)
        except NoSuchWindowException:
            raise TabNotFoundError("there is no active tab to navigate") from None
        _wait_for_title(drv)
        return self._tabinfo(drv, selected=True)

    def get_tab_html(self) -> str:
        # Operates on the focused tab; the caller select_tab's first (uniform
        # tab-targeting contract — no method self-focuses from a handle).
        return self._drv().page_source

    def reload(self) -> TabInfo:
        drv = self._drv()
        self._reset_frames(drv)  # a reload re-fetches the top document — frame focus is gone
        drv.refresh()
        _wait_for_title(drv)
        return self._tabinfo(drv, selected=True)

    def _reset_frames(self, drv) -> None:
        """Drop the focused tab's frame path and return the driver to the top document."""
        if self._focused is not None:
            self._frame_paths[self._focused] = []
        drv.switch_to.default_content()

    # -- frame navigation ---------------------------------------------------

    def get_frame_src(self, css_selector: str) -> dict:
        """Resolve the single visible iframe at ``css_selector``; return its absolute src.

        Runs WITHOUT switching, so the caller can gate the frame's *declared* target
        before entering it. ``src`` is ``None`` for a src-less frame (e.g. one using
        ``srcdoc``). Reuses the click/insert integrity check, and additionally
        refuses a selector that resolves to a non-frame element.
        """
        drv = self._drv()
        el = self._resolve_one_visible(drv, css_selector)
        tag = (el.tag_name or "").lower()
        if tag not in ("iframe", "frame"):
            raise ValueError(f"{css_selector!r} is a <{tag}>, not an iframe/frame")
        src = el.get_attribute("src") or ""
        return {"src": urljoin(drv.current_url, src) if src else None}

    def enter_frame(self, css_selector: str) -> dict:
        """Switch the focused tab into the iframe at ``css_selector`` and record it.

        Returns the frame's actual ``document.URL`` (so the caller can gate the
        *landed* document) and the tab's top-level URL (for the same-origin check).
        The selector is pushed onto this tab's frame path so the focus survives the
        window-refocus every later op performs (see ``_replay_frames``).
        """
        drv = self._drv()
        el = self._resolve_one_visible(drv, css_selector)
        tag = (el.tag_name or "").lower()
        if tag not in ("iframe", "frame"):
            raise ValueError(f"{css_selector!r} is a <{tag}>, not an iframe/frame")
        top_url = drv.current_url  # top-level context URL — unchanged by the switch below
        drv.switch_to.frame(el)
        self._frame_paths.setdefault(self._focused, []).append(css_selector)
        return {"frame_url": drv.execute_script("return document.URL"), "top_url": top_url}

    def switch_to_parent_frame(self) -> dict:
        """Move the focused tab up one frame level (toward the top document).

        Returns the landed frame's ``document.URL`` and the tab's top-level URL so
        the caller can re-gate the ancestor — it may have been navigated to an
        untrusted page since we descended.
        """
        drv = self._drv()
        drv.switch_to.parent_frame()
        path = self._frame_paths.get(self._focused)
        if path:
            path.pop()
        return {"frame_url": drv.execute_script("return document.URL"), "top_url": drv.current_url}

    def switch_to_default_content(self) -> dict:
        """Return the focused tab to its top document, forgetting the frame path.

        Returns the top document's ``document.URL`` (== ``top_url``) so the caller
        can re-gate it — another process may have moved the top page meanwhile.
        """
        drv = self._drv()
        drv.switch_to.default_content()
        self._frame_paths[self._focused] = []
        return {"frame_url": drv.execute_script("return document.URL"), "top_url": drv.current_url}

    def current_url(self) -> str:
        try:
            return self._drv().current_url
        except NoSuchWindowException:
            raise TabNotFoundError("there is no active tab") from None

    def screenshot(self) -> bytes:
        try:
            return self._drv().get_screenshot_as_png()
        except NoSuchWindowException:
            raise TabNotFoundError("there is no active tab to screenshot") from None

    def click_element(self, css_selector: str) -> dict:
        drv = self._drv()
        try:
            url_before = drv.current_url
            el = self._resolve_one_visible(drv, css_selector)
        except NoSuchWindowException:
            raise TabNotFoundError("there is no active tab to click in") from None
        el.click()
        _wait_for_title(drv)
        return {
            "clicked": True,
            "handle": drv.current_window_handle,
            "url_before": url_before,
            "url": drv.current_url,
            "title": drv.title,
        }

    def insert_text_element(self, css_selector: str, value: str) -> dict:
        drv = self._drv()
        try:
            el = self._resolve_one_visible(drv, css_selector)
        except NoSuchWindowException:
            raise TabNotFoundError("there is no active tab to insert text into") from None
        try:
            el.clear()
        except InvalidElementStateException:
            # contenteditable / rich fields don't support clear(); select-all + delete.
            el.send_keys(Keys.CONTROL, "a")
            el.send_keys(Keys.DELETE)
        el.send_keys(value)
        return {
            "inserted": True,
            "value": value,
            "handle": drv.current_window_handle,
            "url": drv.current_url,
            "title": drv.title,
        }
