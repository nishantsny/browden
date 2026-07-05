"""End-to-end: prove that distinct profile dirs run concurrently and in isolation.

A single Selenium session has one focused window, so same-profile work is
serial. But a *separate* ``--user-data-dir`` is a separate Chrome process and a
separate WebDriver session — no shared focus, no shared ``SingletonLock`` — so N
profiles can be driven at the same time. This fires N navigations concurrently
through N independent ``PageSession``s and checks both that they all complete and
that no profile can see another's tabs.

Runs headless (conftest). N Chrome instances launch at once; keep N modest so a
2-core CI runner doesn't thrash.
"""
import asyncio
import urllib.parse

import pytest

from browser_guard.web_navigator.selenium_chrome import SeleniumChromeBackend
from browser_guard.web_navigator.session import PageSession

N = 5


def _data_url(token: str) -> str:
    html = f"<html><head><title>{token}</title></head><body>{token}</body></html>"
    return "data:text/html," + urllib.parse.quote(html)


@pytest.fixture
def sessions(tmp_path):
    backends = [SeleniumChromeBackend(profile_dir=str(tmp_path / f"profile-{i}")) for i in range(N)]
    sess = [PageSession(b, start_reaper=False) for b in backends]
    yield sess
    for b in backends:
        try:
            b._drv().quit()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_concurrent_navigation_across_distinct_profiles(sessions):
    urls = [_data_url(f"PROFILE_{i}") for i in range(N)]

    # Fire all N navigations at once. Each PageSession dispatches its Selenium
    # call through asyncio.to_thread against its own driver, so these run in
    # parallel rather than queueing behind one browser.
    pages = await asyncio.gather(*(s.new_page(u) for s, u in zip(sessions, urls)))

    # Every concurrent request completed and landed on its own page.
    for i, page in enumerate(pages):
        assert f"PROFILE_{i}" in page.url

    # Isolation: each profile sees only its own tab, never a sibling's — proving
    # these are genuinely separate browser sessions, not one shared window.
    listed = await asyncio.gather(*(s.list_pages() for s in sessions))
    for i, my_pages in enumerate(listed):
        urls_seen = " ".join(p["url"] for p in my_pages)
        assert f"PROFILE_{i}" in urls_seen
        sibling = f"PROFILE_{(i + 1) % N}"
        assert sibling not in urls_seen
