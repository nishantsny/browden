"""Async coordinator over a synchronous browser backend.

``BrowserSessionManager`` owns one profile's backend, its soup cache, idle
registry, an in-flight-driver flag, and a periodic reaper task. It's also the
owner of the **customer-facing ids** for its own tabs: constructed with a
``namespace`` (the profile's digest, assigned by the store), it composes every
tab's raw backend handle into an ``id`` of the form ``<namespace>-<handle>`` on
the way out, and splits an incoming ``id`` back to that handle on the way in.
The store routes an id to the right manager; the manager owns the id from there.

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

Tab identity: an ``id`` is required on every method that acts on a specific tab
— ``navigate``, ``force_reload_tab``, and all DOM queries. None of them default
to "the active tab", because the active tab is shared state the human also
controls. An ``id`` that no longer names an open tab surfaces as
``{"error": ..., "id": ...}`` and the dead tab is dropped on the way out.
"""
import asyncio
import time

from ...common.logger import logger
from ...dom import query, serialize
from ..validator.errors import tab_gone_envelope
from ...web_navigator.interface import TabNotFoundError
from ...web_navigator.tab_id import format_tab_id, split_tab_id
from ...web_navigator.registry import TabRegistry
from ...web_navigator.soup_cache import SoupCache

IDLE_TTL_SECONDS = 3600
REAP_INTERVAL_SECONDS = 300


class BrowserSessionManager:
    def __init__(self, backend, *, namespace: str, clock=time.monotonic, start_reaper: bool = True):
        self._backend = backend
        self._namespace = namespace
        self._cache = SoupCache(clock=clock)
        self._registry = TabRegistry(clock=clock)
        self._driver_busy = False
        self._reaper_task: asyncio.Task | None = None
        if start_reaper:
            self.start_reaper()

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def profile_dir(self) -> str:
        return str(self._backend.get_profile_dir())

    # -- id composition ------------------------------------------------------

    def _id(self, handle: str) -> str:
        """The customer-facing composite id for one of this session's tab handles."""
        return format_tab_id(self._namespace, handle)

    @staticmethod
    def _handle(id: str) -> str:
        """The raw backend handle inside a customer id (routed here by the store)."""
        return split_tab_id(id)[1]

    def close(self) -> None:
        """Stop the reaper and tear down this profile's browser.

        Synchronous and best-effort — called from the store's ``atexit`` handler
        at interpreter exit, when the event loop is already stopped. So it must
        not touch the loop (no ``await``/``to_thread``): it just cancels the
        reaper task (a no-op flag once the loop is gone) and drives the backend's
        synchronous ``shutdown`` directly, so the Chrome subprocess this session
        launched doesn't outlive the process.
        """
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        self._backend.shutdown()

    async def is_live(self) -> bool:
        """True if this session's browser is currently running.

        Never launches a browser — unlike the driving methods, which lazily
        (re)start one on first use. Lets aggregators (list_tabs across
        profiles) skip dead sessions instead of resurrecting them.
        """
        return await self._run_driver(self._backend.is_running)

    # -- driver dispatch ----------------------------------------------------

    async def _run_driver(self, fn, *args, **kwargs):
        """Run a synchronous backend call off the loop, flagged as in-flight."""
        self._driver_busy = True
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self._driver_busy = False

    async def _with_tab(self, id: str, work, invalidate: bool = False):
        """Run ``work(handle)`` for a specific tab off the loop, maintaining tracking.

        The one skeleton every per-tab op shared: resolve the ``id`` to its raw
        backend handle, run ``work`` off the event loop (``work`` focuses/uses the
        tab and returns the *finished* result — including any post-processing, since
        it too runs off-loop), and on success touch the tab's registry entry —
        invalidating its cached soup when ``invalidate`` (i.e. the op mutated the
        DOM). If the tab has vanished (``TabNotFoundError``), drop it from cache +
        registry and return the standard tab-gone envelope.

        Every tab op that returns a wire envelope uses this. ``current_url`` is the
        one exception (it returns a bare URL / None, not an envelope) and keeps its
        own tiny try/except.
        """
        handle = self._handle(id)
        try:
            result = await self._run_driver(work, handle)
        except TabNotFoundError:
            self._drop(handle)
            return self._tab_gone(id)
        if invalidate:
            self._cache.invalidate(handle)
        self._registry.touch(handle)
        return result

    def _drop(self, handle: str | None) -> None:
        """Forget a tab — used when it turns out to no longer exist."""
        if handle is not None:
            self._cache.invalidate(handle)
            self._registry.forget(handle)

    def _tab_gone(self, id: str) -> dict:
        logger.warning(f"Requested tab is no longer open: {id}")
        return tab_gone_envelope(id)

    # -- navigation tools ---------------------------------------------------

    async def list_tabs(self) -> list[dict]:
        """Return this profile's open tabs as wire dicts, each with its composite id."""
        self.sweep_idle()
        tabs = await self._run_driver(self._backend.list_tabs)
        logger.info(f"Listed {len(tabs)} tabs")
        result = []
        for t in tabs:
            self._registry.touch(t.handle)
            result.append(t.as_dict(id=self._id(t.handle)))
        return result

    async def new_blank_tab(self, max_tabs: int) -> dict:
        self.sweep_idle()
        def work():
            if len(self._backend.list_handles()) >= max_tabs:
                raise RuntimeError(f"session limit of {max_tabs} tabs reached")
            return self._backend.new_blank_tab()
        tab = await self._run_driver(work)
        logger.info(f"Created new tab: {tab.handle}")
        self._cache.invalidate(tab.handle)
        self._registry.touch(tab.handle)
        return tab.as_dict(id=self._id(tab.handle))

    async def close_tab(self, id: str) -> None:
        self.sweep_idle()
        handle = self._handle(id)
        # Only TabNotFoundError is swallowed: a last-tab ValueError (or any other backend
        # failure) means the tab is still open, so its cache/registry entries must stay.
        try:
            await self._run_driver(self._backend.close_tab, handle)
            logger.info(f"Closed tab: {id}")
        except TabNotFoundError:
            logger.info(f"Attempted to close already-closed tab: {id}")
        self._cache.invalidate(handle)
        self._registry.forget(handle)

    async def select_tab(self, id: str) -> dict:
        self.sweep_idle()

        def work(handle):
            self._backend.select_tab(handle)
            logger.info(f"Selected tab: {id}")
            return {"selected": id}
        return await self._with_tab(id, work)

    async def navigate(self, url: str, *, id: str) -> dict:
        self.sweep_idle()

        def work(handle):
            self._backend.select_tab(handle)
            tab = self._backend.navigate(url)
            logger.info(f"Navigated tab {id} to {url!r}")
            return tab.as_dict(id=self._id(tab.handle))
        return await self._with_tab(id, work, invalidate=True)

    async def document_url(self, *, id: str) -> str | None:
        """Return ``id``'s FOCUSED-document URL (``document.URL``), or None if the tab is gone.

        Reports the document the driver is focused on — the iframe's own URL when focus
        is inside a frame — so the read/write gates validate what is actually being
        read/written, not just the top page. Focuses the tab first (``select_tab``),
        which the frame-navigation tools use to replay the tab's frame focus, so this
        reflects the current frame.

        The one op that doesn't return a wire envelope, so it can't share ``_with_tab``
        (which renders the tab-gone envelope): a gone tab is None here, which the
        caller — the per-action gate — turns into the envelope.
        """
        self.sweep_idle()
        handle = self._handle(id)

        def work():
            self._backend.select_tab(handle)
            return self._backend.document_url()
        try:
            url = await self._run_driver(work)
        except TabNotFoundError:
            self._drop(handle)
            return None
        self._registry.touch(handle)
        return url

    # -- write tools --------------------------------------------------------

    async def click(self, css_selector: str, *, id: str) -> dict:
        """Click the (already policy-validated) add-to-cart element on ``id``.

        The caller (the ``click`` MCP tool) has already gated the host and
        verified the element is a genuine add-to-cart control on the cached
        snapshot. Here we re-find it live and click; the soup cache is then
        invalidated because the DOM has changed.
        """
        self.sweep_idle()

        def work(handle):
            self._backend.select_tab(handle)
            result = self._backend.click_element(css_selector)
            logger.info(f"click: activated {css_selector!r} on tab {id}")
            result.pop("handle", None)
            result["id"] = id
            return result
        return await self._with_tab(id, work, invalidate=True)

    async def insert_text(self, css_selector: str, value: str, *, id: str) -> dict:
        """Type ``value`` into the (already policy-validated) text field on ``id``.

        The caller (the ``insert_text`` MCP tool) has gated the host, verified the element
        is a fillable text control, and matched the field's visible label on the
        cached snapshot. Here we re-find it live and set its value; the soup cache
        is then invalidated because the DOM has changed.
        """
        self.sweep_idle()

        def work(handle):
            self._backend.select_tab(handle)
            result = self._backend.insert_text_element(css_selector, value)
            logger.info(f"insert_text: set {css_selector!r} on tab {id}")
            result.pop("handle", None)
            result["id"] = id
            return result
        return await self._with_tab(id, work, invalidate=True)

    # -- DOM-query tools ----------------------------------------------------

    async def get_element_by_id(self, element_id: str, *, id: str,
                                include_html: bool = False,
                                max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()

        def work(handle):
            soup, reloaded = self._cache.get_soup(handle, self._backend)
            el = query.by_id(soup, element_id)
            return {
                "id": id,
                "reloaded": reloaded,
                "found": el is not None,
                "element": self._node(el, include_html, max_html_bytes),
            }
        return await self._with_tab(id, work, invalidate=False)

    async def get_elements_by_class_name(self, class_names: str, *, id: str,
                                         limit: int = query.LIMIT_DEFAULT, offset: int = 0,
                                         include_html: bool = False,
                                         max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()

        def work(handle):
            soup, reloaded = self._cache.get_soup(handle, self._backend)
            result = query.by_class(soup, class_names, limit, offset)
            return self._list_envelope(id, reloaded, result, include_html, max_html_bytes)
        return await self._with_tab(id, work, invalidate=False)

    async def query_selector(self, css_selector: str, *, id: str,
                             include_html: bool = False,
                             max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()

        def work(handle):
            soup, reloaded = self._cache.get_soup(handle, self._backend)
            try:
                el = query.css_one(soup, css_selector)
            except query.InvalidSelector as e:
                return {"error": f"invalid CSS selector: {e}", "id": id}
            return {
                "id": id,
                "reloaded": reloaded,
                "found": el is not None,
                "element": self._node(el, include_html, max_html_bytes),
            }
        return await self._with_tab(id, work, invalidate=False)

    async def query_selector_all(self, css_selector: str, *, id: str,
                                 limit: int = query.LIMIT_DEFAULT, offset: int = 0,
                                 include_html: bool = False,
                                 max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        self.sweep_idle()

        def work(handle):
            soup, reloaded = self._cache.get_soup(handle, self._backend)
            try:
                result = query.css_all(soup, css_selector, limit, offset)
            except query.InvalidSelector as e:
                return {"error": f"invalid CSS selector: {e}", "id": id}
            return self._list_envelope(id, reloaded, result, include_html, max_html_bytes)
        return await self._with_tab(id, work, invalidate=False)

    async def screenshot(self, *, id: str) -> bytes | dict:
        """Capture a PNG screenshot of ``id``'s viewport.

        Read-only: it focuses the tab and grabs live pixels, so it neither uses
        nor invalidates the soup cache. Returns raw PNG bytes, or the standard
        ``{"error": ..., "id": ...}`` envelope if the tab is gone.
        """
        self.sweep_idle()

        def work(handle):
            self._backend.select_tab(handle)
            png = self._backend.screenshot()
            logger.info(f"Captured screenshot of tab {id} ({len(png)} bytes)")
            return png
        return await self._with_tab(id, work, invalidate=False)

    async def force_reload_tab(self, *, id: str) -> dict:
        self.sweep_idle()

        def work(handle):
            _soup, tab_info = self._cache.force_reload(handle, self._backend)
            return {"id": id, "url": tab_info.url, "title": tab_info.title, "reloaded": True}
        return await self._with_tab(id, work, invalidate=False)

    # -- serialization helpers ---------------------------------------------

    @staticmethod
    def _node(el, include_html: bool, max_html_bytes: int):
        if el is None:
            return None
        return serialize.element_to_node(el, include_html=include_html, max_html_bytes=max_html_bytes)

    def _list_envelope(self, id, reloaded, paginated, include_html, max_html_bytes) -> dict:
        page, eff_limit, eff_offset, total, next_offset = paginated
        elements = [
            serialize.element_to_node(el, include_html=include_html, max_html_bytes=max_html_bytes)
            for el in page
        ]
        return {
            "id": id,
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
        """Reconcile against the live tabs, then close + drop the idle ones. Runs on the loop thread.

        Short-circuits while a driver op is in flight (``_driver_busy``): the next
        tick (or the next tool's lazy sweep) catches everything. The driver calls
        here (``list_handles``, ``close_tab``) are synchronous and briefly block
        the loop — bounded and rare, acceptable; ``list_handles`` is cheap (no
        per-tab focus changes). The last remaining tab is left open (closing the
        only window would quit the driver) but is still dropped from the
        cache/registry so it stops being tracked until touched again.

        Everything here is keyed by raw backend handles (what ``list_handles``
        reports and the registry stores), so no id composition is involved.
        """
        if self._driver_busy:
            return
        # Reconcile: a tab the human closed in Chrome (and the agent never
        # touched again) is gone — drop it from tracking now rather than waiting
        # for its idle TTL to elapse and the close_tab below to no-op on it.
        try:
            live = set(self._backend.list_handles())
        except Exception:
            live = None  # couldn't enumerate; skip reconciliation this tick
        if live is not None:
            for handle in [h for h in self._registry.tracked_ids() if h not in live]:
                logger.info(f"Reconcile: dropping tracked tab closed by human: {handle}")
                self._cache.invalidate(handle)
                self._registry.forget(handle)
        for handle in self._registry.idle_pages(IDLE_TTL_SECONDS, now=now):
            try:
                self._backend.close_tab(handle)
                logger.info(f"Reaper: closed idle tab {handle}")
            except Exception as e:
                # last-tab guard (ValueError), already-closed, dead session — all fine
                logger.debug(f"Reaper: could not close tab {handle}: {e}")
            self._cache.invalidate(handle)
            self._registry.forget(handle)

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
