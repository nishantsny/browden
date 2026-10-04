"""``GatedPage``: the only code that reads a page's content or acts on it.

A ``GatedPage`` is one tab, focused, inside the caller's driver-lock hold. Every
method takes the gate for its request and runs it on the page it is about to
return or touch, in that same hold, so a check and the content it guards can't
come apart. ``BrowserSessionManager`` hands one to each per-tab tool and never
calls the backend's content methods itself; ``test_gated_page_guard`` fails the
build if anything outside this module does.

What each method gates (docs/design/gate-atomicity.md):

* **Reads** gate the live ``document.URL`` before fetching, then gate the URL
  the content was fetched from (``page_snapshot`` reads both in one script).
  The soup cache answers only for the URL it was fetched from.
* **Reloads and navigations** gate where they landed before anything is
  fetched, and bounce an off-list landing to ``about:blank``.
* **Screenshots** gate the URL before the capture and again after it.
* **Writes** gate the URL, then judge the element on a snapshot taken in one
  script with the live match and its URL, and act on that same element.
* **Frame moves** gate the document descended from, the iframe's declared
  ``src`` and the document landed on, in the hold that moves the focus; a
  refused or failed move never leaves the focus inside an unadmitted frame. Any
  move drops the tab's cached snapshots (a ``srcdoc`` frame is judged by its
  parent's URL, so the cache's URL key alone can't tell them apart).
"""
from ...common.logger import logger
from ...dom import query, serialize
from ...web_navigator.interface import FrameFocusError, InvalidSelectorError, TabNotFoundError
from ...web_navigator.soup_cache import SoupCache
from ..validator.errors import ValidationError
from ..validator.read_gates import FrameGate, ReadGate
from ..validator.write_gates import UploadFileGate, WriteGate


class _Bounced(Exception):
    """A reload or navigation landed off-list and was bounced. Never leaves this module."""

    def __init__(self, envelope: dict):
        super().__init__(envelope["error"])
        self.envelope = envelope


class GatedPage:
    def __init__(self, backend, cache: SoupCache, handle: str, id: str):
        """Focus ``handle``. Runs off the loop, inside the caller's driver hold."""
        self._backend = backend
        self._cache = cache
        self._handle = handle
        self._id = id
        backend.select_tab(handle)

    # -- gating helpers -------------------------------------------------------

    def _check_live_url(self, gate: ReadGate | WriteGate) -> str:
        """Gate the focused document's live URL; return it.

        A frame that can't be relied on (lost, or its URL unreadable) means the
        focus has changed under the tab, and every focus change drops the tab's
        cached snapshots: a ``srcdoc`` frame is cached under its parent's URL, so
        the next read at the top would otherwise be answered with the frame's HTML.
        """
        try:
            url = self._backend.document_url()
        except FrameFocusError:
            self._cache.invalidate(self._handle)
            raise
        gate.check_page(url)
        return url

    def _bounce(self, gate: ReadGate, landed: str) -> dict | None:
        """``None`` if ``gate`` admits ``landed``; else bounce the tab to about:blank.

        An open redirect on an allowlisted site, or a server-side 302, can land a
        navigation or reload on a host the gate never saw. Returns the error
        envelope for a bounced landing, and drops any snapshot of the tab.
        ``about:blank`` is always admitted, so the bounce itself re-gates clean.
        """
        try:
            gate.check_page(landed)
            return None
        except ValidationError:
            logger.warning(f"tab {self._id} landed off-allowlist at {landed!r}; bouncing to about:blank")
            self._backend.navigate("about:blank")
            self._cache.invalidate(self._handle)
            return {"error": f"navigation left the allowlist (landed on {landed}) — "
                             f"tab reset to about:blank",
                    "id": self._id, "url": landed}

    def _reload(self, gate: ReadGate):
        """Reload, then gate the landing before anything is fetched; raise ``_Bounced`` if off-list."""
        tab = self._backend.reload()
        bounced = self._bounce(gate, tab.url)
        if bounced is not None:
            raise _Bounced(bounced)
        return tab

    def _fetch(self, gate: ReadGate):
        """Snapshot the page, gate the URL it came from, and cache it under that URL."""
        snap = self._backend.page_snapshot()
        if self._bounce(gate, snap.url) is not None:
            raise ValidationError(f"URL not on the read allowlist: {snap.url}")
        soup = SoupCache.parse(snap.html)
        self._cache.put(self._handle, snap.url, soup)
        return soup

    # -- reads ------------------------------------------------------------------

    def soup(self, gate: ReadGate):
        """``(soup, reloaded)`` for the page, from the cache if it holds a fresh copy of this URL.

        A stale copy is refreshed by reloading the tab in the browser, and that
        reload can be redirected, so its landing is gated before the page is
        fetched: an off-list one is bounced and the read refused.
        """
        url = self._check_live_url(gate)
        entry = self._cache.get(self._handle, url)
        if entry is not None and not self._cache.is_stale(entry):
            return entry.soup, False
        if entry is not None:
            was_in_frame = self._backend.in_frame()
            try:
                self._reload(gate)
            except _Bounced as b:
                raise ValidationError(f"URL not on the read allowlist: {b.envelope['url']}") from None
            if was_in_frame:
                # The reload returned the tab to its top document. Refuse rather
                # than hand back the top page's elements as if they were the
                # frame's — before fetching it, and with the stale entry gone so
                # the next read fetches the top fresh instead of reloading again.
                self._cache.invalidate(self._handle)
                raise FrameFocusError(
                    "the tab's cached snapshot expired and the page was reloaded, which "
                    "returned it to its top document — switch_to_frame again to read "
                    "inside the frame")
        return self._fetch(gate), entry is not None

    def screenshot(self, gate: ReadGate) -> bytes:
        """A PNG of the viewport. The URL is gated before the capture and again after it.

        Inside a frame the focused document is the frame, but the capture is the
        whole viewport — the top page around it included — so the top page's URL
        is gated too, before and after.
        """
        url = self._check_live_url(gate)
        in_frame = self._backend.in_frame()
        if in_frame:
            gate.check_page(self._backend.current_url())
        png = self._backend.screenshot()
        after = self._backend.document_url()
        if after != url:
            gate.check_page(after)
        if in_frame:
            gate.check_page(self._backend.current_url())
        logger.info(f"Captured screenshot of tab {self._id} ({len(png)} bytes)")
        return png

    def reload(self, gate: ReadGate) -> dict:
        """Reload and refresh the cached DOM — gated before, and on the landing.

        A reload returns the tab to its top document, so if the frame the tab was
        focused on is gone (or its URL unreadable), the check before the reload
        judges the top page instead of refusing: reloading is how an agent
        recovers from a lost frame.
        """
        try:
            self._check_live_url(gate)
        except FrameFocusError:
            gate.check_page(self._backend.current_url())
        try:
            tab = self._reload(gate)
        except _Bounced as b:
            return b.envelope
        self._fetch(gate)
        return {"id": self._id, "url": tab.url, "title": tab.title, "reloaded": True}

    def navigate(self, url: str, gate: ReadGate) -> dict:
        """Navigate to ``url`` (already gated by the caller) and gate the landing.

        ``drv.get`` follows 3xx / meta / JS redirects to any final URL, so the
        landing is re-checked, and an off-list one is bounced before any other
        request can see the tab there.
        """
        tab = self._backend.navigate(url)
        logger.info(f"Navigated tab {self._id} to {url!r}")
        self._cache.invalidate(self._handle)
        bounced = self._bounce(gate, tab.url)
        return bounced or tab.as_dict(id=self._id)

    # -- writes -----------------------------------------------------------------

    def _write(self, css_selector: str, gate: WriteGate, act, done: str) -> dict:
        """Gate the page and the element ``css_selector`` picks, then ``act(ref)`` on that element.

        The element is judged on a fresh snapshot, never the soup cache: a cached
        copy can predate what the page shows now. The snapshot's HTML, its URL and
        the live match all come from one script, so the gates judge the very
        element ``act`` then touches.
        """
        url = self._check_live_url(gate)  # gate 1 — before the DOM is even read
        try:
            snap = self._backend.target_snapshot(css_selector)
            page, _, _, total, _ = query.css_all(SoupCache.parse(snap.html), css_selector, 2, 0)
        except (InvalidSelectorError, query.InvalidSelector) as e:
            return {"error": f"invalid CSS selector: {e}", "id": self._id}
        if snap.url != url:
            gate.check_page(snap.url)  # the page moved on its own; judge where it is now
        found = {"total_count": total, "elements": [serialize.element_to_node(el) for el in page]}
        gate.check_element(snap.url, css_selector, found)  # gates 2+, on the parsed snapshot
        # The parse and the live DOM are the same instant, so they agree unless
        # the parser and the browser read the selector differently. Fail closed.
        if snap.count != 1 or total != 1:
            raise ValueError(f"selector {css_selector!r} matched {snap.count} live elements")
        if snap.tag != found["elements"][0]["tag"]:
            raise ValueError(f"selector {css_selector!r} picked a <{snap.tag}> live but a "
                             f"<{found['elements'][0]['tag']}> in the snapshot — refusing")
        result = act(snap.ref)
        logger.info(done)
        result["id"] = self._id
        return result

    def click(self, css_selector: str, gate: WriteGate) -> dict:
        return self._write(css_selector, gate, self._backend.click_target,
                           f"click: activated {css_selector!r} on tab {self._id}")

    def insert_text(self, css_selector: str, value: str, gate: WriteGate) -> dict:
        return self._write(css_selector, gate, lambda ref: self._backend.insert_text_target(ref, value),
                           f"insert_text: set {css_selector!r} on tab {self._id}")

    def upload_file(self, css_selector: str, gate: UploadFileGate) -> dict:
        """Set the file input ``css_selector`` to the file ``gate`` admitted.

        There is no path parameter on purpose. The file is named to the *gate*,
        whose page check — run by ``_write`` before the DOM is read — resolves it
        once and records the result; the backend is handed that exact path. A
        second resolve here could land somewhere the gate never saw, if a symlink
        or directory inside an allowed location were replaced in between, and
        nothing would have checked where it landed. Same discipline as the
        element: act on precisely what was judged.
        """
        return self._write(
            css_selector, gate,
            lambda ref: self._backend.upload_file_target(ref, str(gate.admitted.path)),
            f"upload_file: set {css_selector!r} on tab {self._id}")

    def press_key(self, css_selector: str, key: str, gate: WriteGate) -> dict:
        return self._write(css_selector, gate, lambda ref: self._backend.press_key_target(ref, key),
                           f"press_key: sent {key!r} to {css_selector!r} on tab {self._id}")

    # -- frame focus ------------------------------------------------------------

    def _check_landing(self, gate: FrameGate, result: dict) -> None:
        """Gate where an ascent landed: a frame must be read-allowed and same-origin
        with the top page; the top page itself only has to be read-allowed — there is
        nothing to compare it with, and an opaque top (``about:blank``, ``file://``)
        has no origin to compare anyway."""
        if self._backend.in_frame():
            gate.check_landed(result["top_url"], result["frame_url"])
        else:
            gate.check_page(result["frame_url"])

    def _moved(self, result: dict) -> dict:
        self._cache.invalidate(self._handle)
        result["id"] = self._id
        return result

    def enter_frame(self, css_selector: str, gate: FrameGate) -> dict:
        """Switch into the iframe at ``css_selector``, gated, or not at all.

        The focused document must pass ``gate.check_page``; the backend then
        checks the iframe's declared src (``gate.check_src``) before switching and
        the landed document (``gate.check_landed``: read-allowed + same-origin)
        after, restoring the previous focus on any failure.
        """
        try:
            gate.check_page(self._backend.document_url())
            result = self._backend.enter_frame(css_selector, gate.check_src, gate.check_landed)
        except TabNotFoundError:
            raise  # the tab is gone: the session renders the tab-gone envelope
        except BaseException:
            self._cache.invalidate(self._handle)  # focus may have moved (e.g. a replay reset)
            raise
        logger.info(f"enter_frame: {css_selector!r} on tab {self._id}")
        return self._moved(result)

    def switch_to_parent_frame(self, gate: FrameGate) -> dict:
        """Move up one frame level and re-gate the landing; on refusal, retreat to the top.

        An ancestor may have been navigated elsewhere while focus was deeper.
        """
        try:
            result = self._backend.switch_to_parent_frame()
            self._check_landing(gate, result)
        except TabNotFoundError:
            raise  # the tab is gone: the session renders the tab-gone envelope
        except BaseException:
            self._backend.retreat_to_top()
            self._cache.invalidate(self._handle)
            raise
        logger.info(f"switch_to_parent_frame on tab {self._id}")
        return self._moved(result)

    def switch_to_default_content(self, gate: FrameGate) -> dict:
        """Return to the top document and re-gate it.

        The top page may have moved since focus descended. On refusal the tab is
        already at its top (there is nowhere safer to retreat to); the read tools
        refuse that page too.
        """
        try:
            result = self._backend.switch_to_default_content()
            self._check_landing(gate, result)
        except TabNotFoundError:
            raise  # the tab is gone: the session renders the tab-gone envelope
        except BaseException:
            self._cache.invalidate(self._handle)
            raise
        logger.info(f"switch_to_default_content on tab {self._id}")
        return self._moved(result)
