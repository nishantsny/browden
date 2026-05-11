from abc import ABC, abstractmethod

from ..common.page import PageInfo


class WebNavigatorBackend(ABC):
    """Contract every browser backend must implement.

    Implementations live in browser_guard.web_navigator.<driver>/ and are the
    only place third-party browser libraries (selenium, playwright, ...) are
    imported. The interface itself imports only from common.
    """

    @abstractmethod
    def list_pages(self) -> list[PageInfo]:
        """Return all open tabs."""

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
