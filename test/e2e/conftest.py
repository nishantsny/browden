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
    # The server's _default_profile_dir() reads XDG_CACHE_HOME live, so pointing
    # the env at a throwaway location keeps the test off the user's real profile.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    yield


@pytest.fixture(scope="session")
def mcp_server(tmp_path_factory):
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness
    cache_dir = tmp_path_factory.mktemp("harness_cache")
    harness = McpServerHarness(cache_dir)
    harness.start()
    yield harness
    harness.stop()


@pytest.fixture
def mcp_client_session():
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def _helper(mcp_server):
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        async with sse_client(mcp_server.url) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session
    return _helper
