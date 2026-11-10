"""How finely the post-navigation title wait polls (issue #181).

Selenium's WebDriverWait checks its condition, then sleeps a fixed
``poll_frequency`` -- 0.5s by default. So a title that appears just after the
navigation returns still costs a full tick, and py-spy attributed ~0.25s of
every navigate to that rounding. The condition is one cheap W3C round trip, so
polling it at 50ms pays the real wait instead.

Measured on flipkart.com with a warm connection: the title wait really does take
0.31s there, which a 500ms poll would round up to 0.5s.

Pinned as a structural assertion because the cost is only visible in a profile:
a future refactor of the wait cannot silently hand the rounding back.
"""
from unittest.mock import MagicMock

import browden.web_navigator.selenium_chrome.backend as backend
from browden.web_navigator.selenium_chrome.backend import (
    TITLE_POLL_SECONDS,
    TITLE_WAIT_SECONDS,
    _wait_for_title,
)


# --- #181: how finely the title wait polls --------------------------------

def test_title_wait_polls_far_finer_than_seleniums_default(monkeypatch):
    # Selenium's default poll_frequency is 0.5s, so a title that appears right
    # after the navigation returns still costs a full tick.
    seen = {}

    class FakeWait:
        def __init__(self, drv, timeout, poll_frequency=0.5, *a, **kw):
            seen["timeout"] = timeout
            seen["poll_frequency"] = poll_frequency

        def until(self, condition):
            return condition(MagicMock(title="done"))

    monkeypatch.setattr(backend, "WebDriverWait", FakeWait)
    _wait_for_title(MagicMock(title="done"))

    assert seen["timeout"] == TITLE_WAIT_SECONDS
    assert seen["poll_frequency"] == TITLE_POLL_SECONDS
    assert seen["poll_frequency"] <= 0.05, "a coarser poll is the bug this pins"


def test_title_wait_stays_best_effort(monkeypatch):
    # A page that never sets a title must not fail the navigation — the finer
    # poll changes the granularity of the wait, not its contract.
    class FakeWait:
        def __init__(self, *a, **kw):
            pass

        def until(self, condition):
            raise TimeoutError("no title")

    monkeypatch.setattr(backend, "WebDriverWait", FakeWait)
    _wait_for_title(MagicMock())        # must not raise
