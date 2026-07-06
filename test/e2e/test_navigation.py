# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

import pytest

from browser_guard.web_navigator.selenium_chrome import SeleniumChromeBackend

# A self-contained page so the test is deterministic and offline: a real network
# host can redirect (amazon.com -> www.amazon.com) and break an exact-URL assert.
DATA_URL = "data:text/html,<html><head><title>hi</title></head><body>ok</body></html>"


@pytest.fixture
def backend(tmp_path):
    b = SeleniumChromeBackend(profile_dir=str(tmp_path / "profile"))
    yield b
    b._drv().quit()


def test_new_blank_tab_and_list(backend):
    """Verify new_blank_tab returns a TabInfo for the opened tab and list_tabs includes it."""
    backend.new_blank_tab()
    page = backend.navigate(DATA_URL)
    assert page.url == DATA_URL
    pages = backend.list_tabs()
    assert any(p.per_session_id == page.per_session_id for p in pages)
