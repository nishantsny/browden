"""Async coordinator over a synchronous browser backend.

``BrowserSessionManager`` owns one profile's backend, its soup cache, idle
registry, the lock that serializes its driver, and a periodic reaper task. It's
also the owner of the **customer-facing ids** for its own tabs: constructed with a
``namespace`` (the profile's digest, assigned by the store), it composes every
tab's raw backend handle into an ``id`` of the form ``<namespace>-<handle>`` on
the way out, and splits an incoming ``id`` back to that handle on the way in.
The store routes an id to the right manager; the manager owns the id from there.

Concurrency model (one lock per session):

* One asyncio event loop (FastMCP's). The pure work — cache lookup/invalidate,
  registry touch/forget, query, serialize — runs synchronously on the loop
  thread and is therefore atomic w.r.t. other coroutines.
* Selenium is synchronous and not thread-safe, and a session's tabs share one
  focused window, so every WebDriver call goes through ``_run_driver``: it
  takes this session's ``asyncio.Lock`` and only then hands the work to
  ``asyncio.to_thread(...)``. The unit of mutual exclusion is the whole
  ``work`` callable — which focuses its tab (``select_tab``) and *then* acts —
  so focus-then-act is atomic and a second request cannot steal the focus
  mid-op. Concurrent requests are therefore supported: they queue on the lock,
  each re-focusing its own tab when its turn comes.
* A request that cannot get the lock within ``DRIVER_LOCK_TIMEOUT_SECONDS``
  gives up with ``SessionBusyError`` (surfaced to the agent as an error
  envelope) rather than queueing behind a wedged op forever.
* The lock is **per session**, so requests on different profiles still run
  fully in parallel — one busy profile never blocks another.
* Cleanup drives the browser too, so it takes the same lock: both callers of the
  sweep — the reaper's tick and ``new_blank_tab``'s at-the-cap reclaim — go
  through ``_sweep_idle_locked``. It is best-effort: a sweep that cannot get the
  lock within the timeout is skipped rather than failing its caller.

Tab identity: an ``id`` is required on every method that acts on a specific tab
— ``navigate``, ``force_reload_tab``, and all DOM queries. None of them default
to "the active tab", because the active tab is shared state the human also
controls. An ``id`` that no longer names an open tab surfaces as
``{"error": ..., "id": ...}`` and the dead tab is dropped on the way out.
"""
import asyncio
import contextlib
import time
from collections.abc import Callable

from ...common.logger import logger
from ...dom import query, serialize
from ..validator.allowlist import DEFAULT_REAP_INTERVAL_SECONDS
from ..validator.errors import SessionBusyError, tab_gone_envelope
from ...web_navigator.interface import TabNotFoundError
from ...web_navigator.tab_id import format_tab_id, split_tab_id
from ...web_navigator.registry import TabRegistry
from ...web_navigator.soup_cache import SoupCache

IDLE_TTL_SECONDS = 3600
# How long a request waits for its turn on the session's driver before failing.
DRIVER_LOCK_TIMEOUT_SECONDS = 10


class _AtTabCap(Exception):
    """Internal: the session is full. Never leaves this module.

    Distinct from the ``RuntimeError`` the caller ends up seeing, so the
    reclaim-and-retry in ``new_blank_tab`` can tell "no room" apart from a
    backend failure — and so a backend error can't masquerade as the cap.
    """


class BrowserSessionManager:
    def __init__(self, backend, *, namespace: str, clock=time.monotonic, start_reaper: bool = True,
                 reap_interval_seconds: Callable[[], float] = lambda: DEFAULT_REAP_INTERVAL_SECONDS):
        # reap_interval_seconds is a getter, not a number: the reaper reads it
        # once per tick, so an ``infra.reap_interval_seconds`` edit takes effect
        # on the next wake-up like every other live-reloaded setting, without
        # restarting the server or the session.
        self._backend = backend
        self._namespace = namespace
        self._cache = SoupCache(clock=clock)
        self._registry = TabRegistry(clock=clock)
        self._reap_interval_seconds = reap_interval_seconds
        # Serializes every driver touch in this session (tools AND cleanup).
        self._lock = asyncio.Lock()
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

        The one driver touch that deliberately skips the lock, for the same
        reason: acquiring it needs a running loop, and there isn't one at exit.
        Nothing else can be driving the browser by then either — the loop that
        would have run the other requests is already stopped.
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

    @contextlib.asynccontextmanager
    async def _driver_lock(self, timeout: float = DRIVER_LOCK_TIMEOUT_SECONDS):
        """Hold this session's driver lock, or raise ``SessionBusyError``.

        Every driver touch in the session goes through here, so the section it
        guards is the only thing driving that Chrome for its duration — which is
        what makes "focus the tab, then act on it" safe under concurrent
        requests. Waiters queue in arrival order (``asyncio.Lock`` is FIFO); one
        that is still waiting after ``timeout`` gives up instead of piling up
        behind a wedged op. ``wait_for`` cancels the pending acquire on timeout,
        so a timed-out waiter never leaves the lock held.
        """
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout)
        except TimeoutError:
            logger.warning(
                f"Session busy: gave up waiting {timeout:g}s for the driver "
                f"(profile={self.profile_dir})")
            raise SessionBusyError(timeout) from None
        try:
            yield
        finally:
            self._lock.release()

    async def _run_driver(self, fn, *args, **kwargs):
        """Run a synchronous backend call off the loop, holding the driver lock."""
        async with self._driver_lock():
            return await asyncio.to_thread(fn, *args, **kwargs)

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
        tabs = await self._run_driver(self._backend.list_tabs)
        logger.info(f"Listed {len(tabs)} tabs")
        result = []
        for t in tabs:
            self._registry.touch(t.handle)
            result.append(t.as_dict(id=self._id(t.handle)))
        return result

    async def new_blank_tab(self, max_tabs: int) -> dict:
        """Open a blank tab, reclaiming idle ones first if the session is full.

        The cap is judged on the live tab count, inside the same driver op that
        opens the tab, so the check can't go stale between deciding and acting.

        Hitting it triggers a sweep and one retry. This is the only place that
        sweeps outside the reaper's tick, and deliberately so: the cost lands on
        the rare request that would otherwise fail rather than on every tool
        call, and if the sweep frees nothing it cost one extra ``list_handles``
        on a request that was failing anyway. It also makes the error honest —
        "session limit reached" now means there was nothing idle left to reclaim,
        not merely that the reaper hadn't ticked yet.
        """
        def work():
            if len(self._backend.list_handles()) >= max_tabs:
                raise _AtTabCap
            return self._backend.new_blank_tab()
        try:
            tab = await self._run_driver(work)
        except _AtTabCap:
            logger.info(f"At the {max_tabs}-tab cap: sweeping idle tabs before giving up")
            await self._sweep_idle_locked()
            try:
                tab = await self._run_driver(work)
            except _AtTabCap:
                raise RuntimeError(f"session limit of {max_tabs} tabs reached") from None
        logger.info(f"Created new tab: {tab.handle}")
        self._cache.invalidate(tab.handle)
        self._registry.touch(tab.handle)
        return tab.as_dict(id=self._id(tab.handle))

    async def close_tab(self, id: str) -> None:
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
        def work(handle):
            self._backend.select_tab(handle)
            logger.info(f"Selected tab: {id}")
            return {"selected": id}
        return await self._with_tab(id, work)

    async def navigate(self, url: str, *, id: str) -> dict:
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
        def work(handle):
            self._backend.select_tab(handle)
            result = self._backend.click_element(css_selector)
            logger.info(f"click: activated {css_selector!r} on tab {id}")
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
        def work(handle):
            self._backend.select_tab(handle)
            result = self._backend.insert_text_element(css_selector, value)
            logger.info(f"insert_text: set {css_selector!r} on tab {id}")
            result["id"] = id
            return result
        return await self._with_tab(id, work, invalidate=True)

    async def press_key(self, css_selector: str, key: str, *, id: str) -> dict:
        """Press ``key`` on the (already policy-validated) focused element on ``id``.

        The caller (the ``press_key`` MCP tool) has gated the host, verified the
        element is a focusable control, matched the page label, and checked the key
        is one the rule authorizes. Here we re-find it live, focus it and dispatch
        the key; the soup cache is invalidated because the key may have changed the
        DOM (activated a control, moved a selection).
        """
        def work(handle):
            self._backend.select_tab(handle)
            result = self._backend.press_key_element(css_selector, key)
            logger.info(f"press_key: sent {key!r} to {css_selector!r} on tab {id}")
            result["id"] = id
            return result
        return await self._with_tab(id, work, invalidate=True)

    # -- DOM-query tools ----------------------------------------------------

    async def get_element_by_id(self, element_id: str, *, id: str,
                                include_html: bool = False,
                                max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
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
        def work(handle):
            soup, reloaded = self._cache.get_soup(handle, self._backend)
            result = query.by_class(soup, class_names, limit, offset)
            return self._list_envelope(id, reloaded, result, include_html, max_html_bytes)
        return await self._with_tab(id, work, invalidate=False)

    async def query_selector(self, css_selector: str, *, id: str,
                             include_html: bool = False,
                             max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
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
        def work(handle):
            self._backend.select_tab(handle)
            png = self._backend.screenshot()
            logger.info(f"Captured screenshot of tab {id} ({len(png)} bytes)")
            return png
        return await self._with_tab(id, work, invalidate=False)

    async def invalidate_dom_cache(self, *, id: str) -> dict:
        """Drop ``id``'s cached DOM snapshot without touching the page itself.

        The cheap counterpart to ``force_reload_tab``: nothing is reloaded, so
        whatever the page's own JS built up in the live DOM (an expanded panel, a
        loaded infinite-scroll batch, a half-filled form) survives — only browden's
        parsed copy is thrown away, and the next DOM query re-fetches the live HTML.

        The tab is verified to still exist first, via ``list_handles`` — cheap, and
        unlike ``select_tab`` it doesn't move the focused window — so a tab that is
        gone reports the standard tab-gone envelope rather than silently succeeding.
        """
        def work(handle):
            if handle not in self._backend.list_handles():
                raise TabNotFoundError(f"tab {handle!r} is not open")
            logger.info(f"Invalidated cached DOM for tab {id}")
            return {"id": id, "invalidated": True}
        # invalidate=True is what actually drops the entry (_with_tab does it only
        # once work has succeeded), so a gone tab never reports a bogus success.
        return await self._with_tab(id, work, invalidate=True)

    async def force_reload_tab(self, *, id: str) -> dict:
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

    async def _sweep_idle_locked(self, timeout: float = DRIVER_LOCK_TIMEOUT_SECONDS) -> None:
        """Take the driver lock and sweep — the only way cleanup should be run.

        Cleanup drives the driver too (``list_handles``, ``close_tab``), so it
        has to be serialized with the tools rather than racing them for the
        focused window. Best-effort by design: if the session stays busy for the
        whole timeout, this sweep is skipped (``_driver_lock`` logs it) and the
        next tick catches up. Cleanup falling behind is never worth failing a
        request the agent asked for — including the at-the-cap reclaim, whose
        caller then simply reports the cap as it would have anyway.
        """
        with contextlib.suppress(SessionBusyError):
            async with self._driver_lock(timeout):
                self.sweep_idle()

    def sweep_idle(self, now: float | None = None) -> None:
        """Reconcile against the live tabs, then close + drop the idle ones. Runs on the loop thread.

        **Callers must hold the driver lock** — go through ``_sweep_idle_locked``
        rather than calling this directly. Its two callers are the reaper's tick
        and ``new_blank_tab``'s at-the-cap reclaim; tools deliberately don't
        sweep, so cleanup costs a ``list_handles`` round-trip per interval rather
        than one on every single tool call. The driver calls here
        (``list_handles``, ``close_tab``) are synchronous and briefly block the
        loop — bounded and rare, acceptable; ``list_handles`` is cheap (no
        per-tab focus changes). The last remaining tab is left open (closing the
        only window would quit the driver) but is still dropped from the
        cache/registry so it stops being tracked until touched again.

        Everything here is keyed by raw backend handles (what ``list_handles``
        reports and the registry stores), so no id composition is involved.
        """
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
        """Sweep on a fixed cadence — the only thing that runs the sweep.

        The interval is re-read every tick, so an ``infra.reap_interval_seconds``
        edit lands on the next wake-up without a restart. A sweep that raises
        must never kill the loop: the session would then keep its idle tabs
        forever, so the failure is swallowed and the next tick tries again.
        """
        while True:
            await asyncio.sleep(self._reap_interval_seconds())
            try:
                await self._sweep_idle_locked()
            except Exception:
                pass
