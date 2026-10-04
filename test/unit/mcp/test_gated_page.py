"""``GatedPage`` gates what it actually fetched or acted on, not just what it checked first.

The race table (test_gate_races.py) shows no other *request* can move a tab
between a gate and its read or write. These tests cover what the page itself can
do inside that one hold — its JS navigating between the URL check and the fetch,
or the parse and the live DOM disagreeing — and the soup cache keyed by URL.
"""
import pytest

from browden.common.tab import TabInfo
from browden.dom import query
from browden.mcp.session_management.gated_page import GatedPage
from browden.mcp.validator import ReadGate, ValidationError, WriteGate
from browden.web_navigator.interface import InvalidSelectorError, PageSnapshot, TargetSnapshot
from browden.web_navigator.soup_cache import TTL_SECONDS, SoupCache

SHOP = "https://shop.example/item"
OTHER = "https://shop.example/other"
SECRET = "https://secret.example/inbox"


def _html(text):
    return f"<html><body><p id='x'>{text}</p><button id='go'>Add</button></body></html>"


PAGES = {SHOP: _html("shop"), OTHER: _html("other"), SECRET: _html("SECRET")}


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class PageBackend:
    """One tab at ``url``. ``drift`` is where the page's own JS takes it right after
    the next ``document_url`` — i.e. between the URL check and the fetch."""

    def __init__(self, url=SHOP, redirects=None):
        self.url = url
        self.redirects = redirects or {}
        self.drift = None
        self.calls = []
        self.actions = []
        self.live_count = None  # override the live match count a target snapshot reports
        self.live_tag = None    # override the live tag a target snapshot reports
        self.bad_selector = False

    def _log(self, name):
        self.calls.append((name, self.url))

    def select_tab(self, handle):
        self._log("select_tab")

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
