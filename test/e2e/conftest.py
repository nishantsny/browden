"""Shared setup for the e2e suite: real Chrome, headless, isolated profile.

The backend reads ``BROWSER_GUARD_HEADLESS`` and ``XDG_CACHE_HOME`` when it
launches Chrome, so this autouse fixture sets both *before* any backend is
built. Headless lets the suite run without a display (CI, a server box); a
throwaway profile under a temp ``XDG_CACHE_HOME`` keeps the test out of the
user's real persistent profile and avoids the "user data directory is already
in use" clash with a warm background session.
"""
import pytest


@pytest.fixture(autouse=True)
def _headless_isolated_chrome(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_GUARD_HEADLESS", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # backend.PROFILE_DIR was resolved at import time from the real XDG_CACHE_HOME;
    # repoint it at the throwaway location for the duration of the test.
    from browser_guard.web_navigator.selenium_chrome import backend as backend_mod
    monkeypatch.setattr(
        backend_mod, "PROFILE_DIR",
        tmp_path / "cache" / "browser-guard" / "chrome-profile",
    )
    yield
