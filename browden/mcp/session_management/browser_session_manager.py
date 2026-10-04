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
from ..validator.runtime_configuration import DEFAULT_REAP_INTERVAL_SECONDS
from ..validator.errors import SessionBusyError, ValidationError, tab_gone_envelope
from ..validator.read_gates import FrameGate, ReadGate
from ..validator.write_gates import UploadFileGate, WriteGate
from ...web_navigator.interface import FrameFocusError, TabNotFoundError
from ...web_navigator.tab_id import format_tab_id, split_tab_id
from ...web_navigator.registry import TabRegistry
from ...web_navigator.soup_cache import SoupCache
from .gated_page import GatedPage

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

    async def _with_page(self, id: str, work, invalidate: bool = False):
        """``_with_tab`` for an op on the tab's content: ``work`` gets a :class:`GatedPage`.

        Every read, write, navigation and reload goes through here, so its only
        way to the page is a ``GatedPage`` that gates what it returns or touches.
        """
        return await self._with_tab(
            id, lambda handle: work(GatedPage(self._backend, self._cache, handle, id)), invalidate)

    def _drop(self, handle: str | None) -> None:
        """Forget a tab — used when it turns out to no longer exist."""
        if handle is not None:
            self._cache.invalidate(handle)
            self._registry.forget(handle)

    def _tab_gone(self, id: str) -> dict:
        logger.warning(f"Requested tab is no longer open: {id}")
        return tab_gone_envelope(id)

    # -- navigation tools ---------------------------------------------------

    async def list_tabs(self, *, gate: ReadGate) -> list[dict]:
        """Return this profile's open tabs that ``gate`` admits, as wire dicts.

        H2: a tab parked on a URL the gate refuses is closed, not just hidden, so
        the agent can neither read it nor learn it exists. The listing, the check
        and the close all run in ONE driver-lock hold, so no other request can see
        an off-list tab in between (docs/design/gate-atomicity.md). Closing is
        best-effort: the last tab can't be closed, but it is still left out of the
        listing.
        """
        def work():
            kept, closed = [], []
            for t in self._backend.list_tabs():
                try:
                    gate.check_page(t.url or "")
                except ValidationError:
                    logger.warning(f"list_tabs: closing non-allowlisted tab {t.url!r} (id={self._id(t.handle)})")
                    try:
                        self._close_in_hold(t.handle)
                        closed.append(t.handle)
                    except Exception as e:
                        logger.warning(f"list_tabs: could not close tab {self._id(t.handle)}: {e}")
                    continue
                kept.append(t)
            return kept, closed

        kept, closed = await self._run_driver(work)
        for handle in closed:
            self._drop(handle)
        logger.info(f"Listed {len(kept)} tabs ({len(closed)} off-list closed)")
        result = []
        for t in kept:
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

    def _close_in_hold(self, handle: str) -> None:
        """Close ``handle``'s tab. Runs off the loop, inside the caller's driver hold.

        A tab that is already gone counts as closed. Any other failure (the
        last-tab ``ValueError``, a dead session) raises: the tab is still open, so
        the caller must keep its cache/registry entries. On return the caller drops
        them (``_drop``). The one close used by ``close_tab`` and ``list_tabs``; the
        reaper's sweep has its own, more forgiving one.
        """
        try:
            self._backend.close_tab(handle)
            logger.info(f"Closed tab: {self._id(handle)}")
        except TabNotFoundError:
            logger.info(f"Attempted to close already-closed tab: {self._id(handle)}")

    async def close_tab(self, id: str) -> None:
        handle = self._handle(id)
        await self._run_driver(self._close_in_hold, handle)  # raises if the tab stayed open
        self._drop(handle)

    async def select_tab(self, id: str) -> dict:
        def work(handle):
            self._backend.select_tab(handle)
            logger.info(f"Selected tab: {id}")
            return {"selected": id}
        return await self._with_tab(id, work)

    async def navigate(self, url: str, *, id: str, gate: ReadGate) -> dict:
        """Navigate ``id`` to ``url`` (already gated by the caller) and gate the landing.

        An off-list landing is bounced in the same hold (``GatedPage.navigate``).
        """
        return await self._with_page(id, lambda page: page.navigate(url, gate), invalidate=True)

    # -- reads and writes ---------------------------------------------------
    #
    # A read or write is gated and performed in ONE driver-lock hold, through a
    # ``GatedPage``: it gates the page it returns or acts on, and wherever a
    # navigation or reload in that hold lands, the landing is checked before the
    # hold ends. There is no ungated path: every method requires the ``gate``,
    # and only ``GatedPage`` touches page content. See docs/design/gate-atomicity.md.

    async def click(self, css_selector: str, *, id: str, gate: WriteGate) -> dict:
        """Click ``css_selector`` on ``id`` if ``gate`` authorizes it, in one driver hold.

        The soup cache is then invalidated because the DOM has changed.
        """
        return await self._with_page(id, lambda page: page.click(css_selector, gate), invalidate=True)

    async def insert_text(self, css_selector: str, value: str, *, id: str, gate: WriteGate) -> dict:
        """Type ``value`` into ``css_selector`` on ``id`` if ``gate`` authorizes it, in one driver hold.

        The soup cache is then invalidated because the DOM has changed.
        """
        return await self._with_page(id, lambda page: page.insert_text(css_selector, value, gate),
                                     invalidate=True)

    async def upload_file(self, css_selector: str, *, id: str, gate: UploadFileGate) -> dict:
        """Attach the file ``gate`` admitted to the input ``css_selector`` on ``id``.

        The file travels on the gate, not as an argument — see
        :meth:`GatedPage.upload_file`: the path that was judged is the only one
        anything downstream can reach.

        The soup cache is then invalidated: setting a file input is a DOM change,
        and pages routinely render the chosen filename next to the control.
        """
        return await self._with_page(id, lambda page: page.upload_file(css_selector, gate),
                                     invalidate=True)

    async def press_key(self, css_selector: str, key: str, *, id: str, gate: WriteGate) -> dict:
        """Press ``key`` on ``css_selector`` on ``id`` if ``gate`` authorizes it, in one driver hold.

        ``gate`` must be the one built for this same ``key`` (``press_key_gate``).
        The soup cache is invalidated because the key may have changed the DOM
        (activated a control, moved a selection).
        """
        return await self._with_page(id, lambda page: page.press_key(css_selector, key, gate),
                                     invalidate=True)

    # -- frame navigation ---------------------------------------------------
    #
    # Like a read or a write, a frame move is gated and performed in ONE
    # driver-lock hold: focus the tab, judge where we are and where we're going,
    # move — or roll back. Nothing (a concurrent navigate, a config hot-reload)
    # can come between the verdict and the move, and a refused or failed move
    # never leaves the driver inside a frame that wasn't admitted. See
    # docs/design/gate-atomicity.md.

    async def enter_frame(self, css_selector: str, *, id: str, gate: FrameGate) -> dict:
        """Switch ``id`` into the iframe at ``css_selector``, gated; invalidates the soup cache.

        In one hold: the focused document must pass ``gate.check_page``, the
        frame's declared ``src`` ``gate.check_src``, and the document actually
        landed on ``gate.check_landed`` (read-allowed + same-origin). On any
        failure the backend restores the previous focus and the error propagates.
        """
        def work(handle):
            self._backend.select_tab(handle)
            try:
                gate.check_page(self._backend.document_url())
                result = self._backend.enter_frame(css_selector, gate.check_src, gate.check_landed)
            except TabNotFoundError:
                raise
            except BaseException:
                self._cache.invalidate(handle)  # focus may have moved (e.g. a replay reset)
                raise
            logger.info(f"enter_frame: {css_selector!r} on tab {id}")
            result["id"] = id
            return result
        return await self._with_tab(id, work, invalidate=True)

    async def switch_to_parent_frame(self, *, id: str, gate: FrameGate) -> dict:
        """Move ``id`` up one frame level and re-gate where it lands, in one hold.

        An ancestor may have been navigated elsewhere while we were deeper, so the
        landed document must pass ``gate.check_landed``. On refusal (or a failure
        reading it) the tab retreats to its top document and the error propagates.
        """
        def work(handle):
            self._backend.select_tab(handle)
            try:
                result = self._backend.switch_to_parent_frame()
                gate.check_landed(result["top_url"], result["frame_url"])
            except TabNotFoundError:
                raise
            except BaseException:
                self._backend.retreat_to_top()
                self._cache.invalidate(handle)
                raise
            logger.info(f"switch_to_parent_frame on tab {id}")
            result["id"] = id
            return result
        return await self._with_tab(id, work, invalidate=True)

    async def switch_to_default_content(self, *, id: str, gate: FrameGate) -> dict:
        """Return ``id`` to its top document and re-gate it, in one hold.

        Another process may have moved the top page since we descended. On refusal
        the tab is already at its top document (there is nowhere safer to retreat
        to); the error propagates and the read tools refuse that page too.
        """
        def work(handle):
            self._backend.select_tab(handle)
            try:
                result = self._backend.switch_to_default_content()
                gate.check_landed(result["top_url"], result["frame_url"])
            except TabNotFoundError:
                raise
            except BaseException:
                self._cache.invalidate(handle)
                raise
            logger.info(f"switch_to_default_content on tab {id}")
            result["id"] = id
            return result
        return await self._with_tab(id, work, invalidate=True)

    # -- DOM-query tools ----------------------------------------------------

    async def get_element_by_id(self, element_id: str, *, id: str, gate: ReadGate,
                                include_html: bool = False,
                                max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        def work(page):
            soup, reloaded = page.soup(gate)
            el = query.by_id(soup, element_id)
            return {
                "id": id,
                "reloaded": reloaded,
                "found": el is not None,
                "element": self._node(el, include_html, max_html_bytes),
            }
        return await self._with_page(id, work)

    async def get_elements_by_class_name(self, class_names: str, *, id: str, gate: ReadGate,
                                         limit: int = query.LIMIT_DEFAULT, offset: int = 0,
                                         include_html: bool = False,
                                         max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        def work(page):
            soup, reloaded = page.soup(gate)
            result = query.by_class(soup, class_names, limit, offset)
            return self._list_envelope(id, reloaded, result, include_html, max_html_bytes)
        return await self._with_page(id, work)

    async def query_selector(self, css_selector: str, *, id: str, gate: ReadGate,
                             include_html: bool = False,
                             max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        def work(page):
            soup, reloaded = page.soup(gate)
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
        return await self._with_page(id, work)

    async def query_selector_all(self, css_selector: str, *, id: str, gate: ReadGate,
                                 limit: int = query.LIMIT_DEFAULT, offset: int = 0,
                                 include_html: bool = False,
                                 max_html_bytes: int = serialize.DEFAULT_MAX_HTML_BYTES) -> dict:
        def work(page):
            soup, reloaded = page.soup(gate)
            try:
                result = query.css_all(soup, css_selector, limit, offset)
            except query.InvalidSelector as e:
                return {"error": f"invalid CSS selector: {e}", "id": id}
            return self._list_envelope(id, reloaded, result, include_html, max_html_bytes)
        return await self._with_page(id, work)

    async def screenshot(self, *, id: str, gate: ReadGate) -> bytes | dict:
        """Capture a PNG screenshot of ``id``'s viewport.

        Read-only: it focuses the tab and grabs live pixels, so it neither uses
        nor invalidates the soup cache. Returns raw PNG bytes, or the standard
        ``{"error": ..., "id": ...}`` envelope if the tab is gone.
        """
        return await self._with_page(id, lambda page: page.screenshot(gate))

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

    async def force_reload_tab(self, *, id: str, gate: ReadGate) -> dict:
        """Reload ``id`` and refresh its cached DOM — gated before, and on the landing.

        One hold: the live URL must pass ``gate`` before the reload, and a reload
        redirected off-list is bounced before the hold ends — and before the landed
        page is fetched, so none of it leaves the browser.
        """
        return await self._with_page(id, lambda page: page.reload(gate))

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
