from abc import ABC, abstractmethod
from pathlib import Path

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

    Implementations live in browden.web_navigator.<driver>/ and are the
    only place third-party browser libraries (selenium, playwright, ...) are
    imported. The interface itself imports only from common.

    Any method given a ``handle`` that no longer names an open tab — or any
    method that needs "the active tab" when there isn't one — raises
    ``TabNotFoundError``.

    Tab ids are **opaque, backend-local handles**. A backend knows only its
    own tabs; it neither mints nor understands the customer-facing ids the MCP
    server hands out to agents. The server composes ``<profile>-<handle>`` on
    the way out and splits it back on the way in, so the backend never sees the
    profile mapping — it only ever receives its own raw handles.

    Profile identity: a backend is bound to exactly one profile directory,
    supplied once at construction and immutable thereafter. It is exposed
    read-only via ``get_profile_dir()``; the server asserts (outside the
    backend) that an incoming id's profile matches the backend it routes to.
    """

    @abstractmethod
    def get_profile_dir(self) -> Path:
        """The profile directory (``--user-data-dir``) this backend drives.

        Set once at construction and never changes. The server uses it to
        verify that a composed customer id is routed to the backend whose
        profile it names.
        """

    @abstractmethod
    def is_running(self) -> bool:
        """True if a live browser is currently attached. Must never launch one."""

    @abstractmethod
    def shutdown(self) -> None:
        """Quit the driver and stop the browser this backend launched.

        Synchronous and best-effort: safe to call at interpreter exit (no event
        loop), when nothing was ever launched, or more than once. Releases the
        OS resources the backend owns — the WebDriver session, the Chrome
        subprocess, and its ``SingletonLock`` — which would otherwise outlive
        the process. The backend must not be driven again after this.
        """

    @abstractmethod
    def list_tabs(self) -> list[TabInfo]:
        """Return all open tabs."""

    @abstractmethod
    def list_handles(self) -> list[str]:
        """Return the ids of all open tabs, cheaply (no per-tab metadata / focus changes)."""

    @abstractmethod
    def new_blank_tab(self) -> TabInfo:
        """Open a new empty tab and return it.

        The new tab is selected.
        """

    @abstractmethod
    def close_tab(self, handle: str) -> None:
        """Close a tab by id. Raise if it's the last tab."""

    @abstractmethod
    def select_tab(self, handle: str) -> None:
        """Switch to a tab by id."""

    @abstractmethod
    def navigate(self, url: str) -> TabInfo:
        """Navigate the current tab to url. Url is pre-validated."""

    @abstractmethod
    def get_tab_html(self, handle: str | None = None) -> str:
        """Return the rendered HTML (post-JS DOM) of a tab; active tab if handle is None."""

    @abstractmethod
    def reload(self, handle: str | None = None) -> TabInfo:
        """Reload a tab in the browser; active tab if handle is None."""

    @abstractmethod
    def current_url(self) -> str:
        """Return the URL of the active tab. Caller focuses the tab first."""

    @abstractmethod
    def screenshot(self, handle: str | None = None) -> bytes:
        """Capture a PNG screenshot of a tab's viewport; active tab if handle is None.

        Read-only — never mutates tab state. Returns the raw PNG bytes; the
        caller is responsible for any encoding (e.g. base64 for transport).
        """

    @abstractmethod
    def click_element(self, css_selector: str) -> dict:
        """Find one element by CSS selector on the active tab and click it.

        A write primitive. Re-finds the element *live* (the DOM-query
        tools read a cached snapshot, which cannot click) and refuses unless it
        is the sole match and is displayed + enabled. Returns the pre/post click
        URL and title. Policy — which hosts, which elements — is enforced by the
        caller (the ``click`` tool), never here.
        """

    @abstractmethod
    def insert_text_element(self, css_selector: str, value: str) -> dict:
        """Find one text field by CSS selector on the active tab and set its value.

        The write-text primitive. Like :meth:`click_element`, re-finds the element
        *live* and refuses unless it is the sole match and is displayed + enabled;
        then clears it and types ``value``. Policy — which hosts, which fields,
        what value — is enforced by the caller (the ``insert_text`` tool), never here.
        """
