from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.tab import TabInfo


class TabNotFoundError(LookupError):
    """A backend method was asked to act on a tab id that is no longer open.

    Backend-agnostic: implementations translate their driver's equivalent
    (Selenium's ``NoSuchWindowException``, ...) into this so callers above the
    backend never see a driver-specific exception. Carries a clean message — no
    driver stack trace.
    """


class InvalidSelectorError(ValueError):
    """The browser rejected a CSS selector (``querySelectorAll`` threw)."""


@dataclass(frozen=True)
class PageSnapshot:
    """The focused document's URL and HTML, read together in ONE script.

    Because both come from the same instant, the URL is the one the HTML was
    served from, so gating ``url`` gates exactly what was fetched.
    """
    url: str
    html: str


@dataclass(frozen=True)
class TargetSnapshot:
    """A write target, captured in ONE script with the page it sits on.

    ``url`` and ``html`` are as in :class:`PageSnapshot`; ``count`` is how many
    live elements matched the selector at that instant, ``tag`` the lowercase tag
    name of the first, and ``ref`` an opaque reference to it (``None`` when
    nothing matched). The ref is only good inside the driver hold that took it,
    and is handed back to a ``*_target`` action, which acts on that exact
    element rather than re-finding the selector.
    """
    url: str
    html: str
    count: int
    tag: str | None
    ref: Any


class FrameFocusError(RuntimeError):
    """The tab is focused inside an iframe, but that frame can't be relied on.

    Raised instead of silently falling back to the top document: once focus is
    inside a frame, judging or reading the *top* page in its place would gate one
    document and hand back another. Two cases: the focused frame's own URL can't
    be read (so it can't be gated), and the frame focus was lost to a reload the
    agent didn't ask for. The agent re-enters the frame with ``switch_to_frame``.
    """


class WebNavigatorBackend(ABC):
    """Contract every browser backend must implement.

    Implementations live in safe_agent_browser.web_navigator.<driver>/ and are the
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
    def page_snapshot(self) -> PageSnapshot:
        """The focused document's ``document.URL`` and rendered HTML (post-JS DOM), in
        one script. Caller focuses first."""

    @abstractmethod
    def target_snapshot(self, css_selector: str) -> TargetSnapshot:
        """The focused document plus the live elements ``css_selector`` matches, in
        one script. Caller focuses first. Raises :class:`InvalidSelectorError` if
        the browser rejects the selector."""

    @abstractmethod
    def reload(self) -> TabInfo:
        """Reload the focused tab in the browser. Caller focuses the tab first."""

    @abstractmethod
    def current_url(self) -> str:
        """Return the top-level URL of the focused tab (address-bar URL). Caller focuses first."""

    @abstractmethod
    def document_url(self) -> str:
        """Return the focused *document*'s URL (``document.URL``) — the iframe's own URL
        when focus is inside a frame, else the top URL. Used by the read/write gates so
        they validate the document actually being acted on. Caller focuses first.

        Raises :class:`FrameFocusError` if focus is inside a frame whose URL can't be
        read: the gates must never judge the top page in a frame's place."""

    @abstractmethod
    def screenshot(self) -> bytes:
        """Capture a PNG screenshot of the focused tab's viewport. Caller focuses first.

        Read-only — never mutates tab state. Returns the raw PNG bytes; the
        caller is responsible for any encoding (e.g. base64 for transport).
        """

    @abstractmethod
    def click_target(self, ref) -> dict:
        """Click the element ``ref`` (from :meth:`target_snapshot`).

        A write primitive. Acts on that exact element, never re-finds a selector,
        and refuses unless it is displayed and enabled. Returns the pre/post click
        URL and title. Policy — which hosts, which elements — is judged by the
        caller on the same snapshot the ref came from, never here.
        """

    @abstractmethod
    def insert_text_target(self, ref, value: str) -> dict:
        """Clear the text field ``ref`` (from :meth:`target_snapshot`) and type ``value``.

        Like :meth:`click_target`: that exact element, displayed and enabled, with
        policy judged by the caller.
        """

    @abstractmethod
    def press_key_target(self, ref, key: str) -> dict:
        """Focus the element ``ref`` (from :meth:`target_snapshot`) and press ``key``.

        Like :meth:`click_target`. ``key`` is a W3C ``key`` value such as
        ``"Enter"`` / ``"ArrowDown"``; which keys are allowed is policy, judged by
        the caller.
        """

    @abstractmethod
    def upload_file_target(self, ref, file_path: str) -> dict:
        """Set the file input ``ref`` (from :meth:`target_snapshot`) to ``file_path``.

        Like :meth:`click_target`. ``file_path`` is an absolute path to a regular
        file on this machine; *which* files may be sent anywhere is policy, judged
        by the caller before this is reached.
        """

    @abstractmethod
    def enter_frame(self, css_selector: str, check_src, check_landed) -> dict:
        """Switch the focused tab into the iframe at ``css_selector``, gated, or not at all.

        Resolves the single visible iframe (refusing a non-frame or ambiguous
        selector) and calls ``check_src(src)`` with its absolute declared ``src``
        before switching (skipped for a src-less frame, e.g. ``srcdoc``). Then
        switches and calls ``check_landed(top_url, frame_url)`` on the document it
        actually landed on. If either check raises — or anything else fails after
        the switch — focus is restored to where it was and the exception
        propagates. Only on success is the selector recorded (so the focus survives
        later window-refocus) and ``{"frame_url", "top_url"}`` returned. The checks
        are the caller's policy; this method only guarantees nothing is recorded
        and focus is not left inside a frame that wasn't admitted.
        """

    @abstractmethod
    def switch_to_parent_frame(self) -> dict:
        """Move the focused tab up one frame level; return ``{"frame_url", "top_url"}``."""

    @abstractmethod
    def switch_to_default_content(self) -> dict:
        """Return the focused tab to its top document; return ``{"frame_url", "top_url"}``."""

    @abstractmethod
    def retreat_to_top(self) -> None:
        """Forget the focused tab's frame path and focus its top document. Runs no script."""

    @abstractmethod
    def in_frame(self) -> bool:
        """Whether the focused tab is currently focused inside an iframe."""
