"""Shared setup for the e2e suite: real Chrome, headless, isolated profile.

The backend reads ``BROWDEN_HEADLESS`` and ``XDG_CACHE_HOME`` when it
launches Chrome, so this autouse fixture sets both *before* any backend is
built. Headless lets the suite run without a display (CI, a server box); a
throwaway profile under a temp ``XDG_CACHE_HOME`` keeps the test out of the
user's real persistent profile and avoids the "user data directory is already
in use" clash with a warm background session.
"""
import pytest


@pytest.fixture(autouse=True)
def _headless_isolated_chrome(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWDEN_HEADLESS", "1")
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
def mcp_server_low_caps(tmp_path):
    """A fresh MCP server capped at 2 browser sessions / 2 tabs per session.

    Function-scoped (not shared like ``mcp_server``): the session cap is
    process-global store state, so each cap test needs its own server with an
    empty store — otherwise sessions opened by one test would eat another's
    slots. The tiny 2/2 caps keep the test cheap (at most two real Chromes).
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    allowlist = tmp_path / "low_caps_allowlist.yaml"
    allowlist.write_text(
        'read:\n'
        '  website_overrides:\n'
        '    "*": [".*"]\n'
        'infra:\n'
        '  max_browser_sessions: 2\n'
        '  max_tabs_per_session: 2\n'
    )
    cache_dir = tmp_path / "harness_cache"
    cache_dir.mkdir()  # the harness opens its log file in here, so it must exist
    harness = McpServerHarness(cache_dir, allowlist_path=allowlist)
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
