"""Per-tab cache of parsed HTML (BeautifulSoup), keyed by the URL it was fetched from.

A plain store: it never touches the browser. Fetching, reloading a stale entry
and gating what was fetched are ``GatedPage``'s job
(browden/mcp/session_management/gated_page.py), so every byte of page content
passes a gate on its way in. Synchronous, and only used on one session's driver
thread or event loop, never both at once.

An entry answers only for the URL it was fetched from. A tab whose page's own
JS has navigated it elsewhere misses, so a read can never be answered from a
snapshot of a page the tab has left.

TTL is a duration against ``time.monotonic`` (clock injectable for tests) so a
wall-clock jump can't make a fresh cache look stale.
"""
import time
from dataclasses import dataclass

from ..dependencies.bs4 import BeautifulSoup

TTL_SECONDS = 3600


@dataclass
class CacheEntry:
    url: str
    soup: BeautifulSoup
    fetched_at: float


class SoupCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._entries: dict[str, CacheEntry] = {}

    @staticmethod
    def parse(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def get(self, handle: str, url: str) -> CacheEntry | None:
        """``handle``'s entry if it was fetched from ``url``, else ``None``."""
        entry = self._entries.get(handle)
        return entry if entry is not None and entry.url == url else None

    def is_stale(self, entry: CacheEntry, now: float | None = None) -> bool:
        current = self._clock() if now is None else now
        return current - entry.fetched_at >= TTL_SECONDS

    def put(self, handle: str, url: str, soup: BeautifulSoup, now: float | None = None) -> None:
        current = self._clock() if now is None else now
        self._entries[handle] = CacheEntry(url=url, soup=soup, fetched_at=current)

    def invalidate(self, handle: str) -> None:
        self._entries.pop(handle, None)
