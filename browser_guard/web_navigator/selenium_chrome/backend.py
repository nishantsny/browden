import os
from pathlib import Path

from ...common.logger import logger
from ...common.page import PageInfo
from ...dependencies.selenium import (
    ChromeOptions,
    NoSuchWindowException,
    WebDriverWait,
    webdriver,
)
from ..interface import PageNotFoundError, WebNavigatorBackend


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


PROFILE_DIR = _default_profile_dir()
SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
TITLE_WAIT_SECONDS = 3


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
    """Selenium WebDriver implementation of the navigator backend."""

    def __init__(self):
        self._driver = None

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
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
        if self._driver is None:
            logger.info("Starting new Chrome session")
            _clear_stale_singletons(PROFILE_DIR)
            opts = ChromeOptions()
            opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
            self._driver = webdriver.Chrome(options=opts)
        return self._driver

    def list_pages(self) -> list[PageInfo]:
        drv = self._drv()
        current_handle = drv.current_window_handle
        pages = []
        for handle in drv.window_handles:
            drv.switch_to.window(handle)
            pages.append(PageInfo(
                id=handle,
                url=drv.current_url,
                title=drv.title,
                selected=(handle == current_handle),
            ))
        drv.switch_to.window(current_handle)
        return pages

    def list_page_ids(self) -> list[str]:
        return list(self._drv().window_handles)

    def new_page(self, url: str | None = None) -> PageInfo:
        drv = self._drv()
        drv.switch_to.new_window("tab")
        if url:
            drv.get(url)
            _wait_for_title(drv)
        return PageInfo(
            id=drv.current_window_handle,
            url=drv.current_url,
            title=drv.title,
            selected=True,
        )

    def close_page(self, page_id: str) -> None:
        drv = self._drv()
        if len(drv.window_handles) == 1:
            raise ValueError("Cannot close the last tab")
        _switch(drv, page_id)
        drv.close()

    def select_page(self, page_id: str) -> None:
        _switch(self._drv(), page_id)

    def navigate(self, url: str) -> PageInfo:
        drv = self._drv()
        try:
            drv.get(url)
        except NoSuchWindowException:
            raise PageNotFoundError("there is no active tab to navigate") from None
        _wait_for_title(drv)
        return PageInfo(
            id=drv.current_window_handle,
            url=drv.current_url,
            title=drv.title,
            selected=True,
        )

    def current_page_id(self) -> str:
        try:
            return self._drv().current_window_handle
        except NoSuchWindowException:
            raise PageNotFoundError("there is no active tab") from None

    def get_page_source(self, page_id: str | None = None) -> str:
        drv = self._drv()
        if page_id:
            _switch(drv, page_id)
        return drv.page_source

    def reload(self, page_id: str | None = None) -> PageInfo:
        drv = self._drv()
        if page_id:
            _switch(drv, page_id)
        drv.refresh()
        _wait_for_title(drv)
        return PageInfo(
            id=drv.current_window_handle,
            url=drv.current_url,
            title=drv.title,
            selected=True,
        )
