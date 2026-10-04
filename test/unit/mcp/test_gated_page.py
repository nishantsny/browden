"""``GatedPage`` gates what it actually fetched or acted on, not just what it checked first.

The race table (test_gate_races.py) shows no other *request* can move a tab
between a gate and its read or write. These tests cover what the page itself can
do inside that one hold — its JS navigating between the URL check and the fetch,
or the parse and the live DOM disagreeing — and the soup cache keyed by URL.
"""
import pytest

from safe_agent_browser.common.tab import TabInfo
from safe_agent_browser.dom import query
from safe_agent_browser.mcp.session_management.gated_page import GatedPage
from safe_agent_browser.common.origin import same_origin
from safe_agent_browser.mcp.validator import (
    FrameGate,
    ReadGate,
    UploadFileGate,
    ValidationError,
    WriteGate,
    upload_file_gate,
)
from safe_agent_browser.mcp.validator import SafeAgentBrowserAccessRuleSet
from safe_agent_browser.web_navigator.interface import (
    FrameFocusError,
    InvalidSelectorError,
    PageSnapshot,
    TargetSnapshot,
)
from safe_agent_browser.web_navigator.soup_cache import TTL_SECONDS, SoupCache

SHOP = "https://shop.example/item"
OTHER = "https://shop.example/other"
SECRET = "https://secret.example/inbox"


def _html(text):
    return (f"<html><body><p id='x'>{text}</p><button id='go'>Add</button>"
            f"<input id='up' type='file' aria-label='Receipt'></body></html>")


PAGES = {SHOP: _html("shop"), OTHER: _html("other"), SECRET: _html("SECRET")}


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class PageBackend:
    """One tab at ``url``. ``drift`` is where the page's own JS takes it right after
    the next ``document_url`` — i.e. between the URL check and the fetch.

    ``on_snapshot`` runs during ``target_snapshot``, i.e. after a write's gates
    and before its action — the window an attacker with write access inside an
    allowed upload location would use.
    """

    def __init__(self, url=SHOP, redirects=None):
        self.url = url
        self.redirects = redirects or {}
        self.drift = None
        self.calls = []
        self.actions = []
        self.live_count = None  # override the live match count a target snapshot reports
        self.live_tag = None    # override the live tag a target snapshot reports
        self.bad_selector = False
        self.on_snapshot = None

    def _log(self, name):
        self.calls.append((name, self.url))

    def select_tab(self, handle):
        self._log("select_tab")

    def in_frame(self):
        return False  # no frame model: always at the top document

    def document_url(self):
        self._log("document_url")
        url = self.url
        if self.drift is not None:
            self.url, self.drift = self.drift, None
        return url

    def _tab(self):
        return TabInfo(handle="h1", url=self.url, title="t", selected=True, profile_dir="/p")

    def navigate(self, url):
        self._log("navigate")
        self.url = self.redirects.get(url, url)
        return self._tab()

    def reload(self):
        self._log("reload")
        self.url = self.redirects.get(self.url, self.url)
        return self._tab()

    def page_snapshot(self):
        self._log("page_snapshot")
        return PageSnapshot(url=self.url, html=PAGES.get(self.url, "<html></html>"))

    def screenshot(self):
        self._log("screenshot")
        return b"png:" + self.url.encode()

    def target_snapshot(self, css_selector):
        self._log("target_snapshot")
        if self.on_snapshot is not None:
            self.on_snapshot()
        if self.bad_selector:
            raise InvalidSelectorError(f"{css_selector!r} is not a valid selector")
        html = PAGES.get(self.url, "<html></html>")
        page, _, _, total, _ = query.css_all(SoupCache.parse(html), css_selector, 2, 0)
        return TargetSnapshot(url=self.url, html=html,
                              count=total if self.live_count is None else self.live_count,
                              tag=self.live_tag or (page[0].name if page else None),
                              ref=("ref", self.url, css_selector))

    def click_target(self, ref):
        self.actions.append(("click", ref))
        return {"clicked": True}

    def upload_file_target(self, ref, file_path):
        self.actions.append(("upload", ref, file_path))
        return {"uploaded": True, "file_path": file_path}

    def fetched(self):
        """URLs whose content was snapshotted, in order."""
        return [url for name, url in self.calls if name in ("page_snapshot", "screenshot", "target_snapshot")]


def _read_gate(*allowed):
    def check_page(url):
        if url not in allowed and url != "about:blank":
            raise ValidationError(f"URL not on the read allowlist: {url}")
    return ReadGate(check_page=check_page)


def _write_gate(*allowed):
    def check_page(url):
        if url not in allowed:
            raise ValidationError(f"no click rule authorizes {url}")
    return WriteGate(check_page=check_page, check_element=lambda url, css, found: None)


READ = _read_gate(SHOP, OTHER)


@pytest.fixture
def clock():
    return _Clock()


def _page(backend, cache):
    return GatedPage(backend, cache, "h1", "ns-h1")


# -- reads ---------------------------------------------------------------------

def test_a_first_read_fetches_and_caches_under_the_snapshots_url(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    soup, reloaded = _page(backend, cache).soup(READ)
    assert (soup.find(id="x").text, reloaded) == ("shop", False)
    assert cache.get("h1", SHOP).soup is soup


def test_a_fresh_copy_of_the_same_url_is_served_from_the_cache(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    first, _ = _page(backend, cache).soup(READ)
    again, reloaded = _page(backend, cache).soup(READ)
    assert again is first and reloaded is False
    assert backend.fetched() == [SHOP]


def test_a_tab_its_page_navigated_elsewhere_is_refetched_not_served_the_old_page(clock):
    # SHOP is cached; the page's own JS then takes the tab to OTHER. The read must
    # return OTHER, not the fresh-by-TTL copy of SHOP.
    backend, cache = PageBackend(), SoupCache(clock=clock)
    _page(backend, cache).soup(READ)
    backend.url = OTHER
    soup, reloaded = _page(backend, cache).soup(READ)
    assert (soup.find(id="x").text, reloaded) == ("other", False)


def test_a_stale_copy_is_reloaded(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    _page(backend, cache).soup(READ)
    clock.t += TTL_SECONDS
    _soup, reloaded = _page(backend, cache).soup(READ)
    assert reloaded is True
    assert [name for name, _ in backend.calls].count("reload") == 1


def test_a_stale_reload_that_lands_off_list_is_bounced_before_the_fetch(clock):
    backend, cache = PageBackend(redirects={SHOP: SECRET}), SoupCache(clock=clock)
    _page(backend, cache).soup(READ)
    clock.t += TTL_SECONDS
    with pytest.raises(ValidationError, match="read allowlist"):
        _page(backend, cache).soup(READ)
    assert SECRET not in backend.fetched()
    assert backend.url == "about:blank"
    assert cache.get("h1", SHOP) is None


def test_a_read_refuses_content_its_page_navigated_off_list_to_before_the_fetch(clock):
    # The URL check sees SHOP; by the snapshot, the page's JS is on SECRET. The
    # snapshot carries its own URL, so that is what is gated.
    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.drift = SECRET
    with pytest.raises(ValidationError, match="read allowlist"):
        _page(backend, cache).soup(READ)
    assert backend.url == "about:blank"
    assert cache.get("h1", SECRET) is None


def test_force_reload_that_lands_off_list_is_bounced_before_the_fetch(clock):
    backend, cache = PageBackend(redirects={SHOP: SECRET}), SoupCache(clock=clock)
    result = _page(backend, cache).reload(READ)
    assert "left the allowlist" in result["error"] and result["url"] == SECRET
    assert backend.fetched() == []
    assert backend.url == "about:blank"


def test_force_reload_caches_the_fresh_page(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    result = _page(backend, cache).reload(READ)
    assert result == {"id": "ns-h1", "url": SHOP, "title": "t", "reloaded": True}
    assert cache.get("h1", SHOP) is not None


def test_navigate_drops_the_cached_page_and_bounces_an_off_list_landing(clock):
    backend, cache = PageBackend(redirects={OTHER: SECRET}), SoupCache(clock=clock)
    _page(backend, cache).soup(READ)
    result = _page(backend, cache).navigate(OTHER, READ)
    assert "left the allowlist" in result["error"]
    assert backend.url == "about:blank"
    assert cache.get("h1", SHOP) is None


def test_a_screenshot_is_refused_if_the_page_left_the_allowlist_during_the_capture(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.drift = SECRET
    with pytest.raises(ValidationError, match="read allowlist"):
        _page(backend, cache).screenshot(READ)


def test_a_screenshot_of_a_page_that_stayed_put_is_returned(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    assert _page(backend, cache).screenshot(READ) == b"png:" + SHOP.encode()


# -- writes --------------------------------------------------------------------

def test_a_write_acts_on_the_element_its_snapshot_judged(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    result = _page(backend, cache).click("#go", _write_gate(SHOP))
    assert result == {"clicked": True, "id": "ns-h1"}
    assert backend.actions == [("click", ("ref", SHOP, "#go"))]


def test_a_write_is_judged_on_where_its_page_went_before_the_snapshot(clock):
    # Gate 1 sees SHOP; the page's JS is on OTHER (no write rule) by the
    # snapshot. The snapshot's own URL is re-gated, so nothing is clicked.
    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.drift = OTHER
    with pytest.raises(ValidationError, match="no click rule"):
        _page(backend, cache).click("#go", _write_gate(SHOP))
    assert backend.actions == []


def test_a_write_judges_the_element_on_the_snapshots_url(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.drift = OTHER
    judged = []
    gate = WriteGate(check_page=lambda url: None,
                     check_element=lambda url, css, found: judged.append((url, found["total_count"])))
    _page(backend, cache).click("#go", gate)
    assert judged == [(OTHER, 1)]


def test_a_write_is_refused_when_the_live_dom_has_more_matches_than_the_parse(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.live_count = 2
    with pytest.raises(ValueError, match="matched 2 live elements"):
        _page(backend, cache).click("#go", _write_gate(SHOP))
    assert backend.actions == []


def test_a_write_is_refused_when_the_live_match_is_not_the_parsed_element(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.live_tag = "a"
    with pytest.raises(ValueError, match="refusing"):
        _page(backend, cache).click("#go", _write_gate(SHOP))
    assert backend.actions == []


def test_a_selector_the_browser_rejects_is_an_error_envelope(clock):
    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.bad_selector = True
    result = _page(backend, cache).click("#go", _write_gate(SHOP))
    assert result["error"].startswith("invalid CSS selector") and result["id"] == "ns-h1"
    assert backend.actions == []


def test_a_write_never_reads_a_page_gate_1_refuses(clock):
    backend, cache = PageBackend(url=OTHER), SoupCache(clock=clock)
    with pytest.raises(ValidationError):
        _page(backend, cache).click("#go", _write_gate(SHOP))
    assert backend.fetched() == []


# -- uploads: the file acted on is the file the gate judged ---------------------
#
# `upload-file` is the one action whose subject is not fully described by its
# element: it also has a file, and the browser reads that file LATER, from the
# path it was handed. So the path must be resolved once, by the gate, and handed
# on — never resolved a second time at action time, which could land somewhere
# the gate never saw.

def _upload_rules(location):
    return SafeAgentBrowserAccessRuleSet({
        "upload-file": {"shop.example": {"paths": [".*"], "label": ".*"}},
        "allowed_upload_locations": [str(location)],
    })


def test_an_upload_hands_the_backend_the_path_the_gate_admitted(clock, tmp_path):
    location = tmp_path / "receipts"
    location.mkdir()
    receipt = location / "lunch.png"
    receipt.write_bytes(b"png")

    backend, cache = PageBackend(), SoupCache(clock=clock)
    gate = upload_file_gate(_upload_rules(location), str(receipt))
    result = _page(backend, cache).upload_file("#up", gate)

    assert result["uploaded"] is True
    assert backend.actions == [("upload", ("ref", SHOP, "#up"), str(receipt))]


def test_an_upload_is_not_re_resolved_after_the_gate_admitted_it(clock, tmp_path):
    """The TOCTOU this design closes: resolve once, carry the result.

    A symlink inside an allowed location is admitted (it resolves to a file in
    that location), and is re-pointed at a secret outside it *after* the gate ran
    and before the action — the window a second resolve at action time would fall
    into. The backend must still be handed exactly what was judged.
    """
    location = tmp_path / "receipts"
    location.mkdir()
    (location / "lunch.png").write_bytes(b"png")
    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY")
    link = location / "attach.png"
    link.symlink_to(location / "lunch.png")

    def swap():
        link.unlink()
        link.symlink_to(secret)

    backend, cache = PageBackend(), SoupCache(clock=clock)
    backend.on_snapshot = swap
    gate = upload_file_gate(_upload_rules(location), str(link))
    _page(backend, cache).upload_file("#up", gate)

    sent = backend.actions[-1][2]
    assert sent == str(location / "lunch.png")
    assert "id_rsa" not in sent


def test_an_upload_whose_gate_never_admitted_a_file_is_refused(clock, tmp_path):
    """Fail closed if the page check is ever skipped: no file, no upload."""
    backend, cache = PageBackend(), SoupCache(clock=clock)
    gate = UploadFileGate(check_page=lambda url: None,
                          check_element=lambda url, css, found: None)
    with pytest.raises(ValidationError, match="no file was admitted"):
        _page(backend, cache).upload_file("#up", gate)
    assert backend.actions == []


# -- inside a frame ----------------------------------------------------------------

class FramedBackend(PageBackend):
    """A tab focused inside a frame: ``url`` is the top page, ``frame`` the focused
    document's (effective) URL and ``frame_html`` its content. ``lose()`` makes the
    frame vanish the way the real backend reports it: the tab drops to its top and
    the next ``document_url`` raises ``FrameFocusError`` once."""

    def __init__(self, top=SHOP, frame=SHOP, frame_html=_html("FRAME")):
        super().__init__(top)
        self.frame, self.frame_html = frame, frame_html
        self._in_frame, self._lost = True, False

    def lose(self):
        self._in_frame, self._lost = False, True

    def in_frame(self):
        return self._in_frame

    def document_url(self):
        self._log("document_url")
        if self._lost:
            self._lost = False
            raise FrameFocusError("the frame is gone")
        return self.frame if self._in_frame else self.url

    def current_url(self):
        self._log("current_url")
        return self.url

    def page_snapshot(self):
        self._log("page_snapshot")
        if self._in_frame:
            return PageSnapshot(url=self.frame, html=self.frame_html)
        return super().page_snapshot()

    def switch_to_parent_frame(self):  # one level deep: up lands on the top page
        self._log("switch_to_parent_frame")
        self._in_frame = False
        return {"frame_url": self.url, "top_url": self.url}

    def switch_to_default_content(self):
        self._log("switch_to_default_content")
        self._in_frame, self._lost = False, False
        return {"frame_url": self.url, "top_url": self.url}

    def retreat_to_top(self):
        self._in_frame = False


def test_a_screenshot_inside_a_frame_gates_the_top_page_too():
    # The capture is the whole viewport. An allowed frame on an off-list top page
    # must not get the top page photographed.
    backend = FramedBackend(top=SECRET, frame=SHOP)
    with pytest.raises(ValidationError, match="read allowlist"):
        _page(backend, SoupCache()).screenshot(READ)
    assert "screenshot" not in [name for name, _ in backend.calls]


def test_a_screenshot_inside_a_frame_on_an_allowed_page_is_taken():
    backend = FramedBackend(top=SHOP, frame=OTHER)
    assert _page(backend, SoupCache()).screenshot(READ).startswith(b"png:")


def test_a_lost_srcdoc_frame_is_not_served_from_the_cache_as_the_top_page(clock):
    # A srcdoc frame's URL is its parent's, so its snapshot is cached under the
    # top page's URL. When the frame is lost, that entry must go: the read after
    # the one-time error is the TOP page, and must return the top page's HTML.
    backend, cache = FramedBackend(top=SHOP, frame=SHOP), SoupCache(clock=clock)
    soup, _ = _page(backend, cache).soup(READ)
    assert soup.find(id="x").text == "FRAME"

    backend.lose()
    with pytest.raises(FrameFocusError):
        _page(backend, cache).soup(READ)
    soup, _ = _page(backend, cache).soup(READ)
    assert soup.find(id="x").text == "shop"


FILE_PAGE = "file:///home/u/page.html"


def _frame_gate(read: ReadGate) -> FrameGate:
    """A FrameGate with the real exact-origin rule on top of ``read``."""
    def check_landed(top_url, frame_url):
        read.check_page(frame_url)
        if not same_origin(top_url, frame_url):
            raise ValidationError(f"cross-origin frame refused: {frame_url!r} vs {top_url!r}")
    return FrameGate(check_page=read.check_page, check_src=read.check_page, check_landed=check_landed)


@pytest.mark.parametrize("move", ["switch_to_default_content", "switch_to_parent_frame"])
@pytest.mark.parametrize("top", ["about:blank", FILE_PAGE])
def test_landing_back_on_an_opaque_top_page_needs_only_the_read_check(move, top):
    # about:blank and file:// have no origin, so "same-origin with itself" is
    # false for them — but landing on the top page compares it with nothing: the
    # read check alone decides. (about:blank is always readable; the file page is
    # let in by the gate here, as an override would.)
    backend = FramedBackend(top=top, frame=top)
    result = getattr(_page(backend, SoupCache()), move)(_frame_gate(_read_gate(SHOP, FILE_PAGE)))
    assert result["frame_url"] == top


def test_landing_back_on_an_unreadable_top_page_is_still_refused():
    backend = FramedBackend(top=SECRET, frame=SHOP)
    with pytest.raises(ValidationError, match="read allowlist"):
        _page(backend, SoupCache()).switch_to_default_content(_frame_gate(READ))


def test_force_reload_recovers_from_a_lost_frame_in_one_call(clock):
    # A reload returns the tab to its top document, so a lost frame must not make
    # the reload itself refuse: the check before it judges the top page.
    backend, cache = FramedBackend(top=SHOP, frame=SHOP), SoupCache(clock=clock)
    backend.lose()
    result = _page(backend, cache).reload(READ)
    assert result["reloaded"] is True
    assert "reload" in [name for name, _ in backend.calls]
