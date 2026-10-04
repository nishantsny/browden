from browden.web_navigator.soup_cache import TTL_SECONDS, SoupCache

A = "https://shop.example/a"
B = "https://shop.example/b"


def _soup(text):
    return SoupCache.parse(f"<html><body><p id='x'>{text}</p></body></html>")


def test_an_entry_answers_for_the_url_it_was_fetched_from(fake_clock):
    cache = SoupCache(clock=fake_clock())
    soup = _soup("a")
    cache.put("p1", A, soup)
    assert cache.get("p1", A).soup is soup


def test_an_entry_does_not_answer_for_another_url(fake_clock):
    # The tab's page navigated itself elsewhere: its snapshot of the old page
    # must not answer a read of the new one.
    cache = SoupCache(clock=fake_clock())
    cache.put("p1", A, _soup("a"))
    assert cache.get("p1", B) is None
    assert cache.get("p2", A) is None


def test_put_replaces_the_tabs_entry(fake_clock):
    cache = SoupCache(clock=fake_clock())
    cache.put("p1", A, _soup("a"))
    cache.put("p1", B, _soup("b"))
    assert cache.get("p1", A) is None
    assert cache.get("p1", B).soup.find(id="x").text == "b"


def test_an_entry_goes_stale_after_the_ttl(fake_clock):
    clock = fake_clock()
    cache = SoupCache(clock=clock)
    cache.put("p1", A, _soup("a"))
    entry = cache.get("p1", A)
    clock.t += TTL_SECONDS - 1
    assert not cache.is_stale(entry)
    clock.t += 1
    assert cache.is_stale(entry)


def test_invalidate_drops_the_entry(fake_clock):
    cache = SoupCache(clock=fake_clock())
    cache.put("p1", A, _soup("a"))
    cache.invalidate("p1")
    cache.invalidate("never-cached")  # a no-op
    assert cache.get("p1", A) is None
