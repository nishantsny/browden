import pytest

from browser_guard.web_navigator.selenium_chrome import SeleniumChromeBackend


@pytest.fixture
def backend():
    b = SeleniumChromeBackend()
    yield b
    # Cleanup: close the browser (added in next iteration)


def test_new_page_and_list(backend):
    """Stub: verify new_page returns a PageInfo and list_pages includes it."""
    page = backend.new_page("https://amazon.com")
    assert page.url == "https://amazon.com"
    pages = backend.list_pages()
    assert len(pages) >= 1
