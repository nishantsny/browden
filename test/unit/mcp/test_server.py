import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browden.common.tab import TabInfo


def test_server_instructions_state_concurrency_contract():
    """The single-tab-at-a-time contract must reach the agent via MCP instructions."""
    import browden.mcp.server as server
    ins = (server.mcp.instructions or "").lower()
    assert "concurrent requests" in ins  # the serialize-within-a-session contract
    assert "verbatim" in ins  # spells out the tab-id pass-back contract


def test_tab_entry_point_docs_warn_about_concurrency():
    """new_blank_tab / list_tabs descriptions (what the agent reads) carry the warning."""
    import browden.mcp.server as server
    for name in ("new_blank_tab", "list_tabs"):
        doc = (getattr(server, name).__doc__ or "").lower()
        assert "sequential" in doc or "one tab" in doc
        assert "race" in doc


def test_no_backend_or_session_at_import():
    """Importing server must not construct a backend, a BrowserSessionManager, or a reaper task."""
    with patch("browden.mcp.server.SeleniumChromeBackend") as mock_backend, \
         patch("browden.mcp.session_management.BrowserSessionStore.BrowserSessionManager") as mock_session:
        import browden.mcp.server as server
        importlib.reload(server)
        assert mock_backend.call_count == 0
        assert mock_session.call_count == 0
        assert server._store._sessions == {}


def test_shutdown_registered_once_and_closes_every_session():
    import browden.mcp.session_management.BrowserSessionStore as store_mod
    from browden.web_navigator.selenium_chrome import SeleniumChromeBackend
    with patch.object(store_mod, "atexit") as mock_atexit, \
         patch("browden.mcp.session_management.BrowserSessionStore.BrowserSessionManager",
               side_effect=lambda *a, **k: MagicMock()):
        store = store_mod.BrowserSessionStore()
        # Building three profiles' sessions registers the atexit hook exactly once.
        # (Constructing a backend launches no Chrome, so this stays cheap.)
        sessions = [store.get_or_create_session(SeleniumChromeBackend(f"/p/{i}"), max_sessions=10) for i in range(3)]
        assert mock_atexit.register.call_count == 1
        hook = mock_atexit.register.call_args.args[0]
        assert hook == store._shutdown
        # Firing it closes every session, once each.
        hook()
        for s in sessions:
            s.close.assert_called_once_with()


def test_shutdown_continues_after_one_session_fails():
    import browden.mcp.session_management.BrowserSessionStore as store_mod
    store = store_mod.BrowserSessionStore()
    bad, good = MagicMock(), MagicMock()
    bad.close.side_effect = RuntimeError("driver already dead")
    store._sessions = {"aa": bad, "bb": good}
    store._shutdown()  # must not raise; the good session still gets closed
    bad.close.assert_called_once_with()
    good.close.assert_called_once_with()


def test_get_session_is_lazy_and_cached():
    import browden.mcp.server as server
    importlib.reload(server)
    # Two backends for the same (default) profile map to one cached session;
    # the store never launches Chrome — building a backend is side-effect-free.
    with patch("browden.mcp.session_management.BrowserSessionStore.BrowserSessionManager") as mock_session_cls:
        s1 = server._store.get_or_create_session(server._backend_for(None), max_sessions=10)
        s2 = server._store.get_or_create_session(server._backend_for(None), max_sessions=10)
        assert s1 is s2
        assert mock_session_cls.call_count == 1


def test_distinct_profile_dirs_get_distinct_sessions(tmp_path):
    import browden.mcp.server as server
    importlib.reload(server)
    a, b = tmp_path / "a", tmp_path / "b"
    with patch("browden.mcp.session_management.BrowserSessionStore.BrowserSessionManager",
               side_effect=lambda *a, **k: MagicMock()) as mock_mgr:
        sa1 = server._store.get_or_create_session(server._backend_for(str(a)), max_sessions=10)
        sa2 = server._store.get_or_create_session(server._backend_for(str(a)), max_sessions=10)
        sb = server._store.get_or_create_session(server._backend_for(str(b)), max_sessions=10)
        # same profile -> same cached session; different profile -> different one
        assert sa1 is sa2
        assert sa1 is not sb
        # each session was built on a backend bound to the (resolved) profile asked for
        profiles = {str(c.args[0].get_profile_dir()) for c in mock_mgr.call_args_list}
        assert profiles == {str(a.resolve()), str(b.resolve())}


def test_get_or_create_session_raises_at_the_session_cap(tmp_path):
    """A new profile beyond max_browser_sessions is refused, not launched."""
    import browden.mcp.session_management.BrowserSessionStore as store_mod
    from browden.web_navigator.selenium_chrome import SeleniumChromeBackend
    with patch("browden.mcp.session_management.BrowserSessionStore.BrowserSessionManager",
               side_effect=lambda *a, **k: MagicMock()):
        store = store_mod.BrowserSessionStore()
        store.get_or_create_session(SeleniumChromeBackend(str(tmp_path / "a")), max_sessions=2)
        store.get_or_create_session(SeleniumChromeBackend(str(tmp_path / "b")), max_sessions=2)
        with pytest.raises(RuntimeError, match="limit of 2 reached"):
            store.get_or_create_session(SeleniumChromeBackend(str(tmp_path / "c")), max_sessions=2)
        assert len(store._sessions) == 2  # the rejected profile left no partial session behind


def test_session_cap_counts_distinct_profiles_not_repeat_requests(tmp_path):
    """The cap counts live sessions, not requests: re-requesting a profile that
    already has a session returns the cached one and never raises — even at the
    cap. Only a genuinely new profile beyond the cap is refused."""
    import browden.mcp.session_management.BrowserSessionStore as store_mod
    from browden.web_navigator.selenium_chrome import SeleniumChromeBackend
    a, b, c = (str(tmp_path / p) for p in "abc")
    with patch("browden.mcp.session_management.BrowserSessionStore.BrowserSessionManager",
               side_effect=lambda *a, **k: MagicMock()):
        store = store_mod.BrowserSessionStore()
        sa = store.get_or_create_session(SeleniumChromeBackend(a), max_sessions=2)
        sb = store.get_or_create_session(SeleniumChromeBackend(b), max_sessions=2)
        # Sitting exactly at the cap, repeat requests for existing profiles reuse
        # their session and never trip the limit.
        assert store.get_or_create_session(SeleniumChromeBackend(a), max_sessions=2) is sa
        assert store.get_or_create_session(SeleniumChromeBackend(b), max_sessions=2) is sb
        # Only a brand-new profile beyond the cap is what actually raises.
        with pytest.raises(RuntimeError, match="limit of 2 reached"):
            store.get_or_create_session(SeleniumChromeBackend(c), max_sessions=2)


def test_digest_collision_extends_namespace(tmp_path):
    import hashlib
    import browden.mcp.server as server
    importlib.reload(server)
    key = str(server._resolve_profile_dir(str(tmp_path / "p")))
    full = hashlib.sha256(key.encode()).hexdigest()
    # Another profile already owns this key's 8-char prefix.
    server._store._sessions[full[:8]] = MagicMock()
    assert server._store.digest_for(key) == full[:9]
    assert server._store.digest_for(key) == full[:9]  # memoized, stable across calls


def test_resolve_profile_dir_is_stable_and_distinguishes_dirs(tmp_path):
    import browden.mcp.server as server
    importlib.reload(server)
    # None is stable across calls (so the default profile maps to one session).
    assert server._resolve_profile_dir(None) == server._resolve_profile_dir(None)
    # Distinct dirs yield distinct paths; the same dir is stable.
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    assert server._resolve_profile_dir(a) == server._resolve_profile_dir(a)
    assert server._resolve_profile_dir(a) != server._resolve_profile_dir(b)


def test_default_profile_dir_honours_xdg(monkeypatch, tmp_path):
    import browden.mcp.server as server
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert server._default_profile_dir() == tmp_path / "browden" / "chrome-profile"


def test_default_profile_dir_falls_back_to_home(monkeypatch, tmp_path):
    import browden.mcp.server as server
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert server._default_profile_dir() == tmp_path / ".cache" / "browden" / "chrome-profile"


def _fake_session(**methods):
    s = MagicMock()
    s.is_live = AsyncMock(return_value=True)
    for name, value in methods.items():
        setattr(s, name, AsyncMock(return_value=value))
    return s


@pytest.mark.asyncio
async def test_list_pages_tool_delegates_and_stamps_namespace():
    import browden.mcp.server as server
    importlib.reload(server)
    # The session composes its own tabs' ids and returns finished wire dicts;
    # list_tabs just aggregates them across sessions.
    session = _fake_session(list_tabs=[
        {"id": "pre-h1", "url": "u", "title": "t", "selected": "True", "profile_dir": "/fake/path"}])
    server._store._sessions["pre"] = session
    result = await server.list_tabs()
    assert result == [{"id": "pre-h1", "url": "u", "title": "t", "selected": "True",
                       "profile_dir": "/fake/path"}]
    session.list_tabs.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_pages_skips_dead_sessions_without_driving_them():
    import browden.mcp.server as server
    importlib.reload(server)
    live = _fake_session(list_tabs=[{"id": "aa-h1", "url": "u", "title": "t", "selected": "True", "profile_dir": "/a"}])
    dead = _fake_session(list_tabs=[{"id": "bb-h1", "url": "u", "title": "t", "selected": "True", "profile_dir": "/b"}])
    dead.is_live = AsyncMock(return_value=False)
    server._store._sessions.update({"aa": live, "bb": dead})
    result = await server.list_tabs()
    assert [p["id"] for p in result] == ["aa-h1"]
    # The dead session is skipped entirely — never driven (no Chrome relaunch).
    dead.list_tabs.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_blank_tab_tool_delegates_and_stamps_namespace():
    import browden.mcp.server as server
    importlib.reload(server)
    # The session composes the new tab's id itself and returns the wire dict;
    # the tool just gets the session and returns it.
    session = _fake_session(new_blank_tab={"id": "pre-h1", "url": "u", "title": "t", "selected": "True", "profile_dir": "/p"})
    with patch.object(server._store, "get_or_create_session", return_value=session):
        result = await server.new_blank_tab(profile_dir=None)
    session.new_blank_tab.assert_awaited_once()
    assert result == {"id": "pre-h1", "url": "u", "title": "t", "selected": "True", "profile_dir": "/p"}


@pytest.mark.asyncio
async def test_navigate_tool_validates_then_delegates():
    import browden.mcp.server as server
    importlib.reload(server)
    # The store routes "pre-h1" to its session; the session composes the id in
    # the wire dict it returns.
    session = _fake_session(navigate={"id": "pre-h1", "url": "https://amazon.com", "title": "t", "selected": "True", "profile_dir": "/p"})
    with patch.object(server._store, "route", return_value=session):
        result = await server.navigate("amazon.com", "pre-h1")
    session.navigate.assert_awaited_once_with("https://amazon.com", id="pre-h1")  # normalized by validate_url
    assert result["url"] == "https://amazon.com"
    assert result["id"] == "pre-h1"


@pytest.mark.asyncio
async def test_navigate_tool_rejects_non_allowlisted_read():
    # The shipped sample gates reads by Tranco top-100k; an obscure host is denied
    # before the session is ever routed.
    from browden.mcp.validator import ValidationError
    import browden.mcp.server as server
    importlib.reload(server)
    session = _fake_session(navigate={"id": "pre-h1"})
    with patch.object(server._store, "route", return_value=session):
        with pytest.raises(ValidationError, match="not on allowlist"):
            await server.navigate("nonexistent-xyz-9876.test", "pre-h1")
    session.navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_navigate_tool_requires_page_id():
    import browden.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.navigate("amazon.com")


@pytest.mark.asyncio
async def test_force_reload_page_tool_requires_page_id():
    import browden.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.force_reload_tab()


@pytest.mark.asyncio
async def test_dom_tools_delegate_with_kwargs():
    import browden.mcp.server as server
    importlib.reload(server)
    session = _fake_session(
        get_element_by_id={"found": False, "element": None},
        query_selector_all={"total_count": 0, "elements": []},
        force_reload_tab={"reloaded": True},
    )
    # _route is patched to a fixed (session, handle); the backend always sees the
    # raw handle "h1", never the composite id the tool was called with.
    with patch.object(server._store, "route", return_value=session):
        await server.get_element_by_id("x", "pre-h1", include_html=True, max_html_bytes=10)
        await server.query_selector_all(".a", "pre-h1", limit=3, offset=6)
        await server.force_reload_tab(id="pre-h2")
    session.get_element_by_id.assert_awaited_once_with(
        "x", id="pre-h1", include_html=True, max_html_bytes=10)
    session.query_selector_all.assert_awaited_once_with(
        ".a", id="pre-h1", limit=3, offset=6, include_html=False, max_html_bytes=4096)
    session.force_reload_tab.assert_awaited_once_with(id="pre-h2")


@pytest.mark.asyncio
async def test_dom_tool_requires_page_id():
    import browden.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.query_selector(".a")  # tab_id is required, no "active tab" default


@pytest.mark.asyncio
async def test_screenshot_tool_returns_image():
    import browden.mcp.server as server
    importlib.reload(server)
    png = b"\x89PNG\r\n\x1a\n" + b"fakepixels"
    session = _fake_session(screenshot=png)
    with patch.object(server._store, "route", return_value=session):
        result = await server.screenshot("pre-h1")
    session.screenshot.assert_awaited_once_with(id="pre-h1")
    assert isinstance(result, server.Image)
    # The image carries the raw PNG bytes the session produced.
    assert result.data == png


@pytest.mark.asyncio
async def test_screenshot_tool_passes_through_error_envelope():
    import browden.mcp.server as server
    importlib.reload(server)
    gone = {"error": "tab pre-h9 is no longer open", "id": "pre-h9"}
    session = _fake_session(screenshot=gone)
    with patch.object(server._store, "route", return_value=session):
        result = await server.screenshot("pre-h9")
    assert result == gone  # a dict error is forwarded as-is (id echoed), not an Image


@pytest.mark.asyncio
async def test_screenshot_tool_requires_page_id():
    import browden.mcp.server as server
    importlib.reload(server)
    with pytest.raises(TypeError):
        await server.screenshot()  # tab_id is required, no "active tab" default


def test_route_unknown_page_id_raises():
    import browden.mcp.server as server
    importlib.reload(server)
    with pytest.raises(server.UnknownTabError):
        server._store.route("unknown-1234")


def test_route_selects_session_by_namespace(tmp_path):
    import browden.mcp.server as server
    importlib.reload(server)
    key = str(server._resolve_profile_dir(str(tmp_path / "p")))
    ns = server._store.digest_for(key)
    session = MagicMock()
    server._store._sessions[ns] = session
    # The namespace prefix selects the session; the session itself splits off
    # its own handle (which may contain further dashes) from there.
    assert server._store.route(f"{ns}-CDwindow-ABC123") is session


@pytest.mark.asyncio
async def test_tool_returns_envelope_for_unknown_page_id():
    """The @_tool wrapper converts UnknownTabError into the standard envelope."""
    import browden.mcp.server as server
    importlib.reload(server)
    result = await server.query_selector("body", id="deadbeef-123")
    assert "error" in result
    assert result["id"] == "deadbeef-123"
