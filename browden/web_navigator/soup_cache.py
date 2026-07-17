"""Per-tab cache of parsed HTML (BeautifulSoup) with a TTL-driven auto stale-reload.

**Synchronous by contract** — no ``asyncio`` import. The backend handed in is
synchronous too. ``BrowserSessionManager`` is responsible for only ever calling these
methods inside ``asyncio.to_thread`` (a WebDriver session is not thread-safe,
and a serial MCP client means there's only one such call in flight at a time);
async coordination stays in ``session.py``, never here.

TTL is a duration against ``time.monotonic`` (clock injectable for tests) so a
wall-clock jump can't make a fresh cache look stale.
"""
import time
from dataclasses import dataclass

from ..dependencies.bs4 import BeautifulSoup

TTL_SECONDS = 3600


@dataclass
class CacheEntry:
    soup: BeautifulSoup
    fetched_at: float


class SoupCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._entries: dict[str, CacheEntry] = {}

    @staticmethod
    def _parse(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def get_soup(self, handle: str, backend, now: float | None = None):
        """Return ``(soup, reloaded)`` for ``handle``.

        - missing entry → fetch ``page_source`` and parse; ``reloaded=False`` (the
          tab was just loaded by ``navigate``/``new_blank_tab`` — this isn't "stale").
        - present but older than ``TTL_SECONDS`` → reload the tab in the browser,
          re-fetch, re-parse, store with a fresh timestamp; ``reloaded=True``.
        - present and fresh → the cached soup; ``reloaded=False``.
        """
        current = self._clock() if now is None else now
        entry = self._entries.get(handle)
        if entry is None:
            backend.select_tab(handle)  # focus first: backend ops act on the focused tab
            soup = self._parse(backend.get_tab_html())
            self._entries[handle] = CacheEntry(soup=soup, fetched_at=current)
            return soup, False
        if current - entry.fetched_at >= TTL_SECONDS:
            backend.select_tab(handle)  # focus first: reload + re-fetch act on the focused tab
            backend.reload()
            soup = self._parse(backend.get_tab_html())
            self._entries[handle] = CacheEntry(soup=soup, fetched_at=current)
            return soup, True
        return entry.soup, False

    def invalidate(self, handle: str) -> None:
        self._entries.pop(handle, None)

    def force_reload(self, handle: str, backend, now: float | None = None):
        """Reload the tab in the browser, re-parse, store fresh. Returns ``(soup, tab_info)``."""
        current = self._clock() if now is None else now
        backend.select_tab(handle)  # focus first: reload + re-fetch act on the focused tab
        tab_info = backend.reload()
        soup = self._parse(backend.get_tab_html())
        self._entries[handle] = CacheEntry(soup=soup, fetched_at=current)
        return soup, tab_info
