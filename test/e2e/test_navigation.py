import pytest

from browser_guard.web_navigator.selenium_chrome import SeleniumChromeBackend

# A self-contained page so the test is deterministic and offline: a real network
# host can redirect (amazon.com -> www.amazon.com) and break an exact-URL assert.
DATA_URL = "data:text/html,<html><head><title>hi</title></head><body>ok</body></html>"


@pytest.fixture
def backend():
    b = SeleniumChromeBackend()
    yield b
    b._drv().quit()


def test_new_page_and_list(backend):
    """Verify new_page returns a PageInfo for the opened tab and list_pages includes it."""
    page = backend.new_page(DATA_URL)
    assert page.url == DATA_URL
    pages = backend.list_pages()
    assert any(p.id == page.id for p in pages)
