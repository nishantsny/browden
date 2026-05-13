"""Async coordinator over a synchronous browser backend.

``PageSession`` owns the backend, the soup cache, the idle registry, an
in-flight-driver flag, and a periodic reaper task. It's the only module under
``web_navigator/`` that imports ``asyncio``; the backend and the cache/registry
helpers stay synchronous.

Concurrency model (lockless, no ``threading``):

* One asyncio event loop (FastMCP's). The pure work — cache lookup/invalidate,
  registry touch/forget, query, serialize, and all of ``sweep_idle`` — runs
  synchronously on the loop thread and is therefore atomic w.r.t. other
  coroutines.
* Selenium is synchronous and not thread-safe, so every WebDriver call goes
  through ``await asyncio.to_thread(...)`` (see ``_run_driver``), wrapped by
  ``_driver_busy`` which is assigned on the loop thread immediately around the
  ``await``. ``sweep_idle()`` short-circuits while ``_driver_busy`` is set, so
  the reaper can never issue a command while a tool's driver op is in flight.
* The one case the flag doesn't cover — two *tool* coroutines both reaching
  ``to_thread`` at once — is precluded by the serial MCP stdio client (one
  request in flight at a time), the same assumption the sync tools always made.
  An ``asyncio.Lock`` around the ``to_thread`` section would close that gap but
  is out of scope (and is a lock).
"""
import asyncio
import time

from ..dom import query, serialize
from .registry import PageRegistry
from .soup_cache import SoupCache

IDLE_TTL_SECONDS = 3600
REAP_INTERVAL_SECONDS = 300


class PageSession:
    def __init__(self, backend, *, clock=time.monotonic, start_reaper: bool = True):
        self._backend = backend
        self._cache = SoupCache(clock=clock)
        self._registry = PageRegistry(clock=clock)
        self._driver_busy = False
        self._reaper_task: asyncio.Task | None = None
        if start_reaper:
            self.start_reaper()

    # -- driver dispatch ----------------------------------------------------

    async def _run_driver(self, fn, *args, **kwargs):
        """Run a synchronous backend call off the loop, flagged as in-flight."""
        self._driver_busy = True
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self._driver_busy = False

    async def _load_soup(self, page_id: str | None):
        """Resolve ``page_id`` (None → active tab) and fetch its (maybe stale-reloaded) soup.

        Resolution + cache access happen in one ``to_thread`` hop so the active
        tab can't change underneath us between the two.
        """
        def work():
            pid = page_id if page_id is not None else self._backend.current_page_id()
            soup, reloaded = self._cache.get_soup(pid, self._backend)
            return pid, soup, reloaded
        return await self._run_driver(work)

    # -- navigation tools ---------------------------------------------------

    async def list_pages(self):
        self.sweep_idle()
        pages = await self._run_driver(self._backend.list_pages)
        for p in pages:
            self._registry.touch(p.id)
        return pages

    async def new_page(self, url: str | None = None):
        self.sweep_idle()
        page = await self._run_driver(self._backend.new_page, url)
        self._cache.invalidate(page.id)
        self._registry.touch(page.id)
        return page

    async def close_page(self, page_id: str) -> None:
        self.sweep_idle()
        await self._run_driver(self._backend.close_page, page_id)
        self._cache.invalidate(page_id)
        self._registry.forget(page_id)

    async def select_page(self, page_id: str) -> None:
        self.sweep_idle()
        await self._run_driver(self._backend.select_page, page_id)
        self._registry.touch(page_id)

    async def navigate(self, url: str):
        self.sweep_idle()
        page = await self._run_driver(self._backend.navigate, url)
        self._cache.invalidate(page.id)
        self._registry.touch(page.id)
        return page

    # -- DOM-query tools ----------------------------------------------------

    async def get_element_by_id(self, element_id: str, *, page_id: str | None = None,
                                include_html: bool = False,
                                max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()
        pid, soup, reloaded = await self._load_soup(page_id)
        self._registry.touch(pid)
        el = query.by_id(soup, element_id)
        return {
            "page_id": pid,
            "reloaded": reloaded,
            "found": el is not None,
            "element": self._node(el, include_html, max_html_bytes),
        }

    async def get_elements_by_class_name(self, class_names: str, *, page_id: str | None = None,
                                         limit: int = query.LIMIT_DEFAULT, offset: int = 0,
                                         include_html: bool = False,
                                         max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()
        pid, soup, reloaded = await self._load_soup(page_id)
        self._registry.touch(pid)
        result = query.by_class(soup, class_names, limit, offset)
        return self._list_envelope(pid, reloaded, result, include_html, max_html_bytes)

    async def query_selector(self, css_selector: str, *, page_id: str | None = None,
                             include_html: bool = False,
                             max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()
        pid, soup, reloaded = await self._load_soup(page_id)
        self._registry.touch(pid)
        try:
            el = query.css_one(soup, css_selector)
        except query.InvalidSelector as e:
            return {"error": f"invalid CSS selector: {e}", "page_id": pid}
        return {
            "page_id": pid,
            "reloaded": reloaded,
            "found": el is not None,
            "element": self._node(el, include_html, max_html_bytes),
        }

    async def query_selector_all(self, css_selector: str, *, page_id: str | None = None,
                                 limit: int = query.LIMIT_DEFAULT, offset: int = 0,
                                 include_html: bool = False,
                                 max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()
        pid, soup, reloaded = await self._load_soup(page_id)
        self._registry.touch(pid)
        try:
            result = query.css_all(soup, css_selector, limit, offset)
        except query.InvalidSelector as e:
            return {"error": f"invalid CSS selector: {e}", "page_id": pid}
        return self._list_envelope(pid, reloaded, result, include_html, max_html_bytes)

    async def force_reload_page(self, *, page_id: str | None = None) -> dict:
        self.sweep_idle()

        def work():
            pid = page_id if page_id is not None else self._backend.current_page_id()
            _soup, page_info = self._cache.force_reload(pid, self._backend)
            return pid, page_info
        pid, page_info = await self._run_driver(work)
        self._registry.touch(pid)
        return {"page_id": pid, "url": page_info.url, "title": page_info.title, "reloaded": True}

    # -- serialization helpers ---------------------------------------------

    @staticmethod
    def _node(el, include_html: bool, max_html_bytes: int):
        if el is None:
            return None
        return serialize.element_to_node(el, include_html=include_html, max_html_bytes=max_html_bytes)

    def _list_envelope(self, pid, reloaded, paginated, include_html, max_html_bytes) -> dict:
        page, eff_limit, eff_offset, total, next_offset = paginated
        elements = [
            serialize.element_to_node(el, include_html=include_html, max_html_bytes=max_html_bytes)
            for el in page
        ]
        return {
            "page_id": pid,
            "reloaded": reloaded,
            "total_count": total,
            "offset": eff_offset,
            "limit": eff_limit,
            "returned": len(elements),
            "next_offset": next_offset,
            "elements": elements,
        }

    # -- idle reaper --------------------------------------------------------

    def sweep_idle(self, now: float | None = None) -> None:
        """Close + drop tabs untouched for ``IDLE_TTL_SECONDS``. Runs on the loop thread.

        Short-circuits while a driver op is in flight (``_driver_busy``): the next
        tick (or the next tool's lazy sweep) catches those tabs. The ``close_page``
        calls here are synchronous and briefly block the loop — bounded and rare,
        acceptable. The last remaining tab is left open (closing the only window
        would quit the driver) but is dropped from the cache/registry so it stops
        being tracked until touched again.
        """
        if self._driver_busy:
            return
        for pid in self._registry.idle_pages(IDLE_TTL_SECONDS, now=now):
            try:
                self._backend.close_page(pid)
            except Exception:
                pass  # last-tab guard (ValueError), already-closed, dead session — all fine
            self._cache.invalidate(pid)
            self._registry.forget(pid)

    def start_reaper(self) -> asyncio.Task:
        """Create the periodic reaper task (idempotent). Requires a running event loop."""
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
        return self._reaper_task

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL_SECONDS)
            try:
                self.sweep_idle()
            except Exception:
                pass
