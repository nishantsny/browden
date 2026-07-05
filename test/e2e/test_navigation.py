import pytest

from browser_guard.web_navigator.selenium_chrome import SeleniumChromeBackend

# A self-contained tab so the test is deterministic and offline: a real network
# host can redirect (amazon.com -> www.amazon.com) and break an exact-URL assert.
DATA_URL = "data:text/html,<html><head><title>hi</title></head><body>ok</body></html>"


@pytest.fixture
def backend(tmp_path):
    b = SeleniumChromeBackend(profile_dir=str(tmp_path / "profile"), id_namespace="e2e")
    yield b
    b._drv().quit()


def test_new_page_and_list(backend):
    """Verify new_blank_tab returns a TabInfo for the opened tab and list_tabs includes it."""
    tab = backend.new_blank_tab()
    assert tab.url == "about:blank"
    tabs = backend.list_tabs()
    assert any(p.id == tab.id for p in tabs)
