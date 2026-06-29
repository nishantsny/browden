from abc import ABC, abstractmethod

from ..common.page import PageInfo


class PageNotFoundError(LookupError):
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

    Any method given a ``page_id`` that no longer names an open tab — or any
    method that needs "the active tab" when there isn't one — raises
    ``PageNotFoundError``.
    """

    @abstractmethod
    def list_pages(self) -> list[PageInfo]:
        """Return all open tabs."""

    @abstractmethod
    def list_page_ids(self) -> list[str]:
        """Return the ids of all open tabs, cheaply (no per-tab metadata / focus changes)."""

    @abstractmethod
    def new_page(self, url: str | None = None) -> PageInfo:
        """Open a new tab, optionally navigating to url."""

    @abstractmethod
    def close_page(self, page_id: str) -> None:
        """Close a tab by id. Raise if it's the last tab."""

    @abstractmethod
    def select_page(self, page_id: str) -> None:
        """Switch to a tab by id."""

    @abstractmethod
    def navigate(self, url: str) -> PageInfo:
        """Navigate the current tab to url. Url is pre-validated."""

    @abstractmethod
    def current_page_id(self) -> str:
        """Return the id of the currently active tab."""

    @abstractmethod
    def get_page_source(self, page_id: str | None = None) -> str:
        """Return the rendered HTML (post-JS DOM) of a tab; active tab if page_id is None."""

    @abstractmethod
    def reload(self, page_id: str | None = None) -> PageInfo:
        """Reload a tab in the browser; active tab if page_id is None."""

    @abstractmethod
    def current_url(self) -> str:
        """Return the URL of the active tab. Caller focuses the tab first."""

    @abstractmethod
    def click_element(self, css_selector: str) -> dict:
        """Find one element by CSS selector on the active tab and click it.

        The only write primitive. Re-finds the element *live* (the DOM-query
        tools read a cached snapshot, which cannot click) and refuses unless it
        is the sole match and is displayed + enabled. Returns the pre/post click
        URL and title. Policy — which hosts, which elements — is enforced by the
        caller (the ``add_to_cart`` tool), never here.
        """
