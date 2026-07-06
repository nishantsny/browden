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

    def get_soup(self, tab_id: str, backend, now: float | None = None):
        """Return ``(soup, reloaded)`` for ``tab_id``.

        - missing entry → fetch ``page_source`` and parse; ``reloaded=False`` (the
          tab was just loaded by ``navigate``/``new_blank_tab`` — this isn't "stale").
        - present but older than ``TTL_SECONDS`` → reload the tab in the browser,
          re-fetch, re-parse, store with a fresh timestamp; ``reloaded=True``.
        - present and fresh → the cached soup; ``reloaded=False``.
        """
        current = self._clock() if now is None else now
        entry = self._entries.get(tab_id)
        if entry is None:
            soup = self._parse(backend.get_page_source(tab_id))
            self._entries[tab_id] = CacheEntry(soup=soup, fetched_at=current)
            return soup, False
        if current - entry.fetched_at >= TTL_SECONDS:
            backend.reload(tab_id)
            soup = self._parse(backend.get_page_source(tab_id))
            self._entries[tab_id] = CacheEntry(soup=soup, fetched_at=current)
            return soup, True
        return entry.soup, False

    def invalidate(self, tab_id: str) -> None:
        self._entries.pop(tab_id, None)

    def force_reload(self, tab_id: str, backend, now: float | None = None):
        """Reload the tab in the browser, re-parse, store fresh. Returns ``(soup, page_info)``."""
        current = self._clock() if now is None else now
        page_info = backend.reload(tab_id)
        soup = self._parse(backend.get_page_source(tab_id))
        self._entries[tab_id] = CacheEntry(soup=soup, fetched_at=current)
        return soup, page_info
