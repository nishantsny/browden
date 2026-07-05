import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ...common.logger import logger
from ...common.page import PageInfo
from ...dependencies.selenium import (
    By,
    ChromeOptions,
    NoSuchElementException,
    NoSuchWindowException,
    WebDriverWait,
    webdriver,
)
from ..interface import PageNotFoundError, WebNavigatorBackend
from ..page_id import SEPARATOR, format_page_id, split_page_id
from ..utils.network_utils import get_free_port


def _switch(drv, page_id: str) -> None:
    """Focus a tab by id, translating Selenium's missing-window error.

    Selenium's NoSuchWindowException stringifies to a multi-line driver stack
    trace; PageNotFoundError carries a clean, actionable message instead.
    """
    try:
        drv.switch_to.window(page_id)
    except NoSuchWindowException:
        raise PageNotFoundError(f"tab {page_id!r} is not open") from None


def _default_profile_dir() -> Path:
    """Resolve the Chrome profile path, respecting XDG_CACHE_HOME."""
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "browser-guard" / "chrome-profile"


def _headless_enabled() -> bool:
    """Whether to launch Chrome headless, controlled by ``BROWSER_GUARD_HEADLESS``.

    A real human-facing session wants a visible window, so this defaults to off.
    Set ``BROWSER_GUARD_HEADLESS=1`` (or true/yes/on) for environments without a
    display — e2e tests and CI runners.
    """
    return os.environ.get("BROWSER_GUARD_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")


PROFILE_DIR = _default_profile_dir()
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


def _find_chrome_binary() -> str:
    """Locate the Chrome/Chromium executable to launch directly.

    Honours ``BROWSER_GUARD_CHROME_BINARY`` (or the common ``CHROME_BIN``)
    first, then falls back to the usual binary names on PATH.
    """
    explicit = os.environ.get("BROWSER_GUARD_CHROME_BINARY") or os.environ.get("CHROME_BIN")
    if explicit:
        return explicit
    for name in CHROME_BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "Could not find a Chrome/Chromium binary on PATH; "
        "set BROWSER_GUARD_CHROME_BINARY to its full path."
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
        # origin unless this is set; Selenium's attach needs it.
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if _headless_enabled():
        # New headless mode + the flags a sandboxed CI container needs.
        logger.info("Launching Chrome headless")
        args += [
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1280,1024",
        ]
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

    drv.get() returns when the load event fires, but many pages set their
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
    picks the path (the MCP server resolves the shared default via
    ``_profile_key``); distinct profiles don't share the per-dir
    ``SingletonLock``, so two backends on two profiles run concurrently without
    clobbering each other's window focus.
    """

    def __init__(self, profile_dir, *, id_namespace: str):
        # Both are required. profile_dir binds this backend to exactly one
        # Chrome --user-data-dir (the caller resolves the shared default path;
        # the backend never falls back to a module default). id_namespace is
        # stamped onto every public page_id so the MCP server can route an
        # incoming id back to this session; it must be non-empty and free of
        # the separator, or ids wouldn't round-trip.
        if not profile_dir:
            raise ValueError("profile_dir is required")
        if not id_namespace:
            raise ValueError("id_namespace is required and must be non-empty")
        if SEPARATOR in id_namespace:
            raise ValueError(
                f"id_namespace must not contain {SEPARATOR!r}: {id_namespace!r}")
        self._driver = None
        self._chrome_proc = None
        self._profile_dir = Path(profile_dir).expanduser()
        self.id_namespace = id_namespace

    def _get_page_id(self, handle: str) -> str:
        return format_page_id(self.id_namespace, handle)

    def _internal(self, page_id: str) -> str:
        namespace, handle = split_page_id(page_id)
        if namespace != self.id_namespace:
            raise PageNotFoundError(f"invalid page_id prefix for {page_id!r}")
        return handle

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

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

    def is_running(self) -> bool:
        """True if a live Chrome is currently attached. Probes without launching.

        The observing counterpart to ``_drv()``'s heal-by-relaunch: callers that
        only want to look (list_pages across profiles) must not spawn a browser
        as a side effect.
        """
        if self._driver is None:
            return False
        try:
            _ = self._driver.window_handles
            return True
        except Exception:
            self._teardown()
            return False

    def _drv(self):
        if self._driver is not None:
            try:
                # 1. Check if the driver session is alive
                _ = self._driver.window_handles

                # 2. Check if the CURRENT window context is still valid
                # This is specifically what causes the "no such window" error later
                _ = self._driver.current_window_handle
            except Exception:
                # If either check fails, the state is bad; clean up and restart
                self._teardown()
        if self._driver is None:
            profile = self.profile_dir
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

    def list_pages(self) -> list[PageInfo]:
        drv = self._drv()
        current_handle = drv.current_window_handle
        pages = []
        for handle in drv.window_handles:
            drv.switch_to.window(handle)
            pages.append(PageInfo(
                id=self._get_page_id(handle),
                url=drv.current_url,
                title=drv.title,
                selected=(handle == current_handle),
            ))
        drv.switch_to.window(current_handle)
        return pages

    def list_page_ids(self) -> list[str]:
        return [self._get_page_id(h) for h in self._drv().window_handles]

    def new_page(self, url: str | None = None) -> PageInfo:
        drv = self._drv()
        drv.switch_to.new_window("tab")
        if url:
            drv.get(url)
            _wait_for_title(drv)
        return PageInfo(
            id=self._get_page_id(drv.current_window_handle),
            url=drv.current_url,
            title=drv.title,
            selected=True,
        )

    def close_page(self, page_id: str) -> None:
        drv = self._drv()
        if len(drv.window_handles) == 1:
            raise ValueError("Cannot close the last tab")
        _switch(drv, self._internal(page_id))
        drv.close()
        # drv.close() leaves the driver focused on the now-dead handle. The next
        # command — or _drv()'s health check, which reads current_window_handle —
        # would then mistake the session for dead and relaunch the whole browser,
        # taking every other tab with it. Re-focus a survivor to keep the session
        # valid.
        remaining = drv.window_handles
        if remaining:
            drv.switch_to.window(remaining[0])

    def select_page(self, page_id: str) -> None:
        _switch(self._drv(), self._internal(page_id))

    def navigate(self, url: str) -> PageInfo:
        drv = self._drv()
        try:
            drv.get(url)
        except NoSuchWindowException:
            raise PageNotFoundError("there is no active tab to navigate") from None
        _wait_for_title(drv)
        return PageInfo(
            id=self._get_page_id(drv.current_window_handle),
            url=drv.current_url,
            title=drv.title,
            selected=True,
        )

    def current_page_id(self) -> str:
        try:
            return self._get_page_id(self._drv().current_window_handle)
        except NoSuchWindowException:
            raise PageNotFoundError("there is no active tab") from None

    def get_page_source(self, page_id: str | None = None) -> str:
        drv = self._drv()
        if page_id:
            _switch(drv, self._internal(page_id))
        return drv.page_source

    def reload(self, page_id: str | None = None) -> PageInfo:
        drv = self._drv()
        if page_id:
            _switch(drv, self._internal(page_id))
        drv.refresh()
        _wait_for_title(drv)
        return PageInfo(
            id=self._get_page_id(drv.current_window_handle),
            url=drv.current_url,
            title=drv.title,
            selected=True,
        )

    def current_url(self) -> str:
        try:
            return self._drv().current_url
        except NoSuchWindowException:
            raise PageNotFoundError("there is no active tab") from None

    def screenshot(self, page_id: str | None = None) -> bytes:
        drv = self._drv()
        if page_id:
            _switch(drv, self._internal(page_id))
        try:
            return drv.get_screenshot_as_png()
        except NoSuchWindowException:
            raise PageNotFoundError("there is no active tab to screenshot") from None

    def click_element(self, css_selector: str) -> dict:
        drv = self._drv()
        try:
            url_before = drv.current_url
            matches = drv.find_elements(By.CSS_SELECTOR, css_selector)
        except NoSuchWindowException:
            raise PageNotFoundError("there is no active tab to click in") from None
        # Ambiguity is a deny: the policy layer validated exactly one element on the
        # snapshot, so more (or fewer) live matches means the DOM moved under us.
        if len(matches) == 0:
            raise NoSuchElementException(f"no element matches {css_selector!r}")
        if len(matches) > 1:
            raise ValueError(f"selector {css_selector!r} matched {len(matches)} live elements")
        el = matches[0]
        if not el.is_displayed():
            raise ValueError("target element is not visible")
        if not el.is_enabled():
            raise ValueError("target element is disabled")
        el.click()
        _wait_for_title(drv)
        return {
            "clicked": True,
            "page_id": self._get_page_id(drv.current_window_handle),
            "url_before": url_before,
            "url": drv.current_url,
            "title": drv.title,
        }
