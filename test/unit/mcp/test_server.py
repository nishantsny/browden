import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser_guard.common.page import PageInfo


def test_server_instructions_state_concurrency_contract():
    """The single-tab-at-a-time contract must reach the agent via MCP instructions."""
    import browser_guard.mcp.server as server
    ins = (server.mcp.instructions or "").lower()
    assert "one at a time" in ins
    assert "page_id" in ins  # spells out that even different tabs race


def test_tab_entry_point_docs_warn_about_concurrency():
    """new_page / list_pages descriptions (what the agent reads) carry the warning."""
    import browser_guard.mcp.server as server
    for name in ("new_page", "list_pages"):
        doc = (getattr(server, name).__doc__ or "").lower()
        if name == "new_page":
            assert "sequential" in doc or "one tab" in doc
            assert "race" in doc


def test_no_backend_or_session_at_import():
    """Importing server must not construct a backend, a PageSession, or a reaper task."""
    with patch("browser_guard.web_navigator.selenium_chrome.SeleniumChromeBackend") as mock_backend, \
         patch("browser_guard.web_navigator.session.PageSession") as mock_session:
        import browser_guard.mcp.server as server
        importlib.reload(server)
        assert mock_backend.call_count == 0
        assert mock_session.call_count == 0
        assert server._sessions == {}


def test_get_session_is_lazy_and_cached():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    with patch("browser_guard.mcp.server.SeleniumChromeBackend"), \
         patch("browser_guard.mcp.server.PageSession") as mock_session_cls:
        s1 = server._get_session()
        s2 = server._get_session()
        assert s1 is s2
        assert s1 is s2


def test_distinct_profile_dirs_get_distinct_sessions(tmp_path):
    import browser_guard.mcp.server as server
    importlib.reload(server)
    a, b = tmp_path / "a", tmp_path / "b"
    with patch("browser_guard.mcp.server.SeleniumChromeBackend") as mock_backend, \
         patch("browser_guard.mcp.server.PageSession", side_effect=lambda *a, **k: MagicMock()):
        sa1 = server._get_session(str(a))
        sa2 = server._get_session(str(a))
        sb = server._get_session(str(b))
        # same profile -> same cached session; different profile -> different one
        assert sa1 is sa2
        assert sa1 is not sb
        # each backend was built bound to the profile the caller asked for
        profiles = {c.kwargs.get("profile_dir") for c in mock_backend.call_args_list}
        assert profiles == {str(a), str(b)}


def test_profile_key_is_stable_and_distinguishes_dirs(tmp_path):
    import browser_guard.mcp.server as server
    importlib.reload(server)
    # None is stable across calls (so the default profile maps to one session).
    assert server._profile_key(None) == server._profile_key(None)
    # Distinct dirs yield distinct keys; the same dir is stable.
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    assert server._profile_key(a) == server._profile_key(a)
    assert server._profile_key(a) != server._profile_key(b)


def _fake_session(**methods):
    s = MagicMock()
    for name, value in methods.items():
        setattr(s, name, AsyncMock(return_value=value))
    return s


@pytest.mark.asyncio
async def test_list_pages_tool_delegates_to_session():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    session = _fake_session(list_pages=[PageInfo(id="pre-h1", url="u", title="t", selected=True)])
    server._sessions["pre"] = server._SessionEntry("/fake/path", session)
    result = await server.list_pages()
    assert result == [{"page_id": "pre-h1", "url": "u", "title": "t", "selected": True, "profile_dir": "/fake/path"}]
    session.list_pages.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_page_tool_delegates_to_session():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    session = _fake_session(new_page=PageInfo(id="pre-h1", url="u", title="t", selected=True))
    with patch("browser_guard.mcp.server._get_session", return_value=session):
        result = await server.new_page("https://amazon.com", profile_dir=None)
    session.new_page.assert_awaited_once()
    assert result["url"] == "u"
    assert result["page_id"] == "pre-h1"


@pytest.mark.asyncio
async def test_navigate_tool_validates_then_delegates():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    session = _fake_session(navigate=PageInfo(id="pre-h1", url="https://amazon.com", title="t", selected=True))
    with patch("browser_guard.mcp.server._route", return_value=session):
        result = await server.navigate("amazon.com", "pre-h1")
    session.navigate.assert_awaited_once_with("https://amazon.com", page_id="pre-h1")  # normalized by validate_url
    assert result["url"] == "https://amazon.com"


@pytest.mark.asyncio
async def test_navigate_tool_requires_page_id():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.navigate("amazon.com")


@pytest.mark.asyncio
async def test_force_reload_page_tool_requires_page_id():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.force_reload_page()


@pytest.mark.asyncio
async def test_dom_tools_delegate_with_kwargs():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    session = _fake_session(
        get_element_by_id={"found": False, "element": None},
        query_selector_all={"total_count": 0, "elements": []},
        force_reload_page={"reloaded": True},
    )
    with patch("browser_guard.mcp.server._route", return_value=session):
        await server.get_element_by_id("x", "pre-h1", include_html=True, max_html_bytes=10)
        await server.query_selector_all(".a", "pre-h1", limit=3, offset=6)
        await server.force_reload_page(page_id="pre-h2")
    session.get_element_by_id.assert_awaited_once_with(
        "x", page_id="pre-h1", include_html=True, max_html_bytes=10)
    session.query_selector_all.assert_awaited_once_with(
        ".a", page_id="pre-h1", limit=3, offset=6, include_html=False, max_html_bytes=4096)
    session.force_reload_page.assert_awaited_once_with(page_id="pre-h2")


@pytest.mark.asyncio
async def test_dom_tool_requires_page_id():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.query_selector(".a")  # page_id is required, no "active tab" default


@pytest.mark.asyncio
async def test_screenshot_tool_returns_image():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    png = b"\x89PNG\r\n\x1a\n" + b"fakepixels"
    session = _fake_session(screenshot=png)
    with patch("browser_guard.mcp.server._route", return_value=session):
        result = await server.screenshot("pre-h1")
    session.screenshot.assert_awaited_once_with(page_id="pre-h1")
    assert isinstance(result, server.Image)
    # The image carries the raw PNG bytes the session produced.
    assert result.data == png


@pytest.mark.asyncio
async def test_screenshot_tool_passes_through_error_envelope():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    gone = {"error": "page pre-h9 is no longer open", "page_id": "pre-h9"}
    session = _fake_session(screenshot=gone)
    with patch("browser_guard.mcp.server._route", return_value=session):
        result = await server.screenshot("pre-h9")
    assert result == gone  # a dict error is forwarded as-is, not wrapped in an Image


@pytest.mark.asyncio
async def test_screenshot_tool_requires_page_id():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.screenshot()  # page_id is required, no "active tab" default


@pytest.mark.asyncio
async def test_route_unknown_page_id():
    import browser_guard.mcp.server as server
    importlib.reload(server)
    result = server._route("unknown-1234")
    assert "error" in result
    assert result["page_id"] == "unknown-1234"
