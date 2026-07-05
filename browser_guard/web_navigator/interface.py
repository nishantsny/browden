from abc import ABC, abstractmethod

from ..common.tab import TabInfo


class TabNotFoundError(LookupError):
    """A backend method was asked to act on a tab id that is no longer open.

    Backend-agnostic: implementations translate their driver's equivalent
    (Selenium's ``NoSuchWindowException``, ...) into this so callers above the
    backend never see a driver-specific exception. Carries a clean message — no
    driver stack trace.
    """


class WebNavigatorBackend(ABC):
    """Contract every browser backend must implement.

    Implementations live in browser_guard.web_navigator.<driver>/ and are the
    only place third-party browser libraries (selenium, playwright, ...) are
    imported. The interface itself imports only from common.

    Any method given a ``tab_id`` that no longer names an open tab — or any
    method that needs "the active tab" when there isn't one — raises
    ``TabNotFoundError``.

    Public tab ids: every backend is constructed with a required, non-empty
    ``id_namespace`` and must emit and accept ids in the
    ``tab_id.format_page_id(namespace, handle)`` form — the MCP server routes
    an id back to its session by splitting it with the same helper. The
    namespace makes ids globally unique across the concurrently-running
    per-profile backends the server holds.
    """

    @abstractmethod
    def is_running(self) -> bool:
        """True if a live browser is currently attached. Must never launch one."""

    @abstractmethod
    def list_tabs(self) -> list[TabInfo]:
        """Return all open tabs."""

    @abstractmethod
    def list_tab_ids(self) -> list[str]:
        """Return the ids of all open tabs, cheaply (no per-tab metadata / focus changes)."""

    @abstractmethod
    def new_blank_tab(self) -> TabInfo:
        """Open a new tab, optionally navigating to url."""

    @abstractmethod
    def close_tab(self, tab_id: str) -> None:
        """Close a tab by id. Raise if it's the last tab."""

    @abstractmethod
    def select_tab(self, tab_id: str) -> None:
        """Switch to a tab by id."""

    @abstractmethod
    def navigate(self, url: str) -> TabInfo:
        """Navigate the current tab to url. Url is pre-validated."""

    @abstractmethod
    def current_page_id(self) -> str:
        """Return the id of the currently active tab."""

    @abstractmethod
    def get_page_source(self, tab_id: str | None = None) -> str:
        """Return the rendered HTML (post-JS DOM) of a tab; active tab if tab_id is None."""

    @abstractmethod
    def reload(self, tab_id: str | None = None) -> TabInfo:
        """Reload a tab in the browser; active tab if tab_id is None."""

    @abstractmethod
    def current_url(self) -> str:
        """Return the URL of the active tab. Caller focuses the tab first."""

    @abstractmethod
    def screenshot(self, tab_id: str | None = None) -> bytes:
        """Capture a PNG screenshot of a tab's viewport; active tab if tab_id is None.

        Read-only — never mutates tab state. Returns the raw PNG bytes; the
        caller is responsible for any encoding (e.g. base64 for transport).
        """

    @abstractmethod
    def click_element(self, css_selector: str) -> dict:
        """Find one element by CSS selector on the active tab and click it.

        The only write primitive. Re-finds the element *live* (the DOM-query
        tools read a cached snapshot, which cannot click) and refuses unless it
        is the sole match and is displayed + enabled. Returns the pre/post click
        URL and title. Policy — which hosts, which elements — is enforced by the
        caller (the ``add_to_cart`` tool), never here.
        """
