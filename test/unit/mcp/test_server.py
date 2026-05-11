import importlib
from unittest.mock import patch


def test_backend_not_instantiated_at_import():
    """Importing server must not construct a SeleniumChromeBackend."""
    with patch(
        "browser_guard.web_navigator.selenium_chrome.SeleniumChromeBackend"
    ) as mock_cls:
        import browser_guard.mcp.server as server
        importlib.reload(server)
        assert mock_cls.call_count == 0
        assert server._backend is None


def test_get_backend_is_lazy_and_cached():
    import browser_guard.mcp.server as server
    importlib.reload(server)

    with patch(
        "browser_guard.mcp.server.SeleniumChromeBackend"
    ) as mock_cls:
        b1 = server._get_backend()
        b2 = server._get_backend()
        assert b1 is b2
        assert mock_cls.call_count == 1
