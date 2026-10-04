from browden.common.tab import TabInfo
from browden.web_navigator.soup_cache import TTL_SECONDS, SoupCache


class FakeBackend:
    def __init__(self):
        self.source = "<html><body><p id='x'>hi</p></body></html>"
        self.get_calls = 0
        self.reload_calls = 0
        self.focused = None  # last tab select_tab focused

    def select_tab(self, handle):
        self.focused = handle

    def get_tab_html(self):
        self.get_calls += 1
        return self.source

    def reload(self):
        self.reload_calls += 1
        return TabInfo(handle=self.focused or "active", url="https://www.amazon.com/", title="T", selected=True, profile_dir="/p")


def test_first_get_parses_without_reloading(fake_clock):
    backend = FakeBackend()
    cache = SoupCache(clock=fake_clock())
    soup, reloaded = cache.get_soup("p1", backend)
    assert reloaded is False
    assert backend.reload_calls == 0
    assert backend.get_calls == 1
    assert soup.find(id="x").text == "hi"


def test_fresh_entry_returns_cached_without_backend_hit(fake_clock):
    backend = FakeBackend()
    cache = SoupCache(clock=fake_clock())
    s1, _ = cache.get_soup("p1", backend)
    s2, reloaded = cache.get_soup("p1", backend)
    assert s2 is s1
    assert reloaded is False
    assert backend.get_calls == 1  # not re-fetched


def test_stale_entry_triggers_reload(fake_clock):
    backend = FakeBackend()
    clock = fake_clock()
    cache = SoupCache(clock=clock)
    cache.get_soup("p1", backend)
    clock.t += TTL_SECONDS
    backend.source = "<html><body><p id='y'>new</p></body></html>"
    soup, reloaded = cache.get_soup("p1", backend)
    assert reloaded is True
    assert backend.reload_calls == 1
    assert soup.find(id="y").text == "new"


def test_invalidate_forces_refetch(fake_clock):
    backend = FakeBackend()
    cache = SoupCache(clock=fake_clock())
    cache.get_soup("p1", backend)
    cache.invalidate("p1")
    _soup, reloaded = cache.get_soup("p1", backend)
    assert reloaded is False  # a fresh load, not a "stale reload"
    assert backend.get_calls == 2
    assert backend.reload_calls == 0


def test_force_reload_reloads_browser_and_returns_page_info(fake_clock):
    backend = FakeBackend()
    cache = SoupCache(clock=fake_clock())
    cache.get_soup("p1", backend)
    backend.source = "<html><body><p id='z'>fresh</p></body></html>"
    soup, tab_info = cache.force_reload("p1", backend)
    assert backend.reload_calls == 1
    assert soup.find(id="z").text == "fresh"
    assert tab_info.url == "https://www.amazon.com/"
    # and the cache now holds the fresh soup
    again, reloaded = cache.get_soup("p1", backend)
    assert reloaded is False
    assert again is soup


class _Refused(Exception):
    pass


def _refuse(_tab_info):
    raise _Refused


def test_a_stale_reload_runs_on_reload_before_fetching_and_can_abort_it(fake_clock):
    # on_reload sees where the reload landed before any of that page is fetched;
    # raising from it stops the fetch and leaves nothing new in the cache.
    backend = FakeBackend()
    clock = fake_clock()
    cache = SoupCache(clock=clock)
    old, _ = cache.get_soup("p1", backend)
    clock.t += TTL_SECONDS
    seen = []
    try:
        cache.get_soup("p1", backend, on_reload=lambda tab: (seen.append((tab.url, backend.get_calls)), _refuse(tab)))
    except _Refused:
        pass
    else:
        raise AssertionError("on_reload's refusal did not propagate")
    assert seen == [("https://www.amazon.com/", 1)]  # landed URL, and no fetch since the first
    assert backend.get_calls == 1


def test_force_reload_runs_on_reload_before_fetching_and_can_abort_it(fake_clock):
    backend = FakeBackend()
    cache = SoupCache(clock=fake_clock())
    try:
        cache.force_reload("p1", backend, on_reload=_refuse)
    except _Refused:
        pass
    else:
        raise AssertionError("on_reload's refusal did not propagate")
    assert backend.reload_calls == 1
    assert backend.get_calls == 0  # the landed page was never fetched
