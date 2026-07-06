"""The per-profile browser-session store and the customer<->backend id mapping.

One :class:`BrowserSessionManager` (hence one Chrome process) per profile
directory. Requests that share a profile share its session and run serially
through it; requests on *different* profiles get independent Chrome sessions and
run concurrently, since distinct ``--user-data-dir`` profiles don't share window
focus or the per-dir ``SingletonLock``. The default (``profile_dir=None``) is
resolved to the shared default profile *path* (via :meth:`profile_key`) before
the backend is built, so existing single-profile behaviour is unchanged.

Sessions are keyed by a per-profile namespace (a digest of the profile path).
The server — not the backend — composes the customer-facing ``id`` as
``<namespace>-<handle>`` on the way out and splits it back on the way in, so
:meth:`route` can map an incoming id to its session.
"""
import atexit
import hashlib
from pathlib import Path

from ...common.logger import logger
from ...web_navigator.page_id import split_page_id
from ...web_navigator.selenium_chrome import SeleniumChromeBackend
from ...web_navigator.selenium_chrome import backend as selenium_backend
from .BrowserSessionManager import BrowserSessionManager


class UnknownTabError(LookupError):
    """An id whose namespace matches no active session."""

    def __init__(self, id: str):
        super().__init__(f"unknown id {id!r}")
        self.envelope = {
            "error": "unknown id — its profile has no active session; call new_blank_tab to start one (or list_tabs)",
            "id": id,
        }


class BrowserSessionStore:
    """Owns the per-profile ``BrowserSessionManager``s and the id<->session routing."""

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSessionManager] = {}
        self._digests: dict[str, str] = {}
        self._atexit_registered = False

    def profile_key(self, profile_dir: str | None) -> str:
        """Canonical registry key for a profile dir; None -> the default profile's path.

        Reads the backend module's ``PROFILE_DIR`` for the default rather than
        constructing a backend, so it stays cheap and side-effect-free (and works
        when the backend class is mocked in tests).
        """
        if profile_dir:
            return str(Path(profile_dir).expanduser().resolve())
        return str(selenium_backend.PROFILE_DIR)

    def digest_for(self, key: str) -> str:
        """Mint (and memoize) the id namespace for a profile path.

        Normally the first 8 hex chars of the path's sha256; on the astronomically
        rare prefix collision with an already-registered profile, the digest is
        extended until unique, so a collision costs a longer id — never an error.
        """
        digest = self._digests.get(key)
        if digest is None:
            full = hashlib.sha256(key.encode()).hexdigest()
            n = 8
            while full[:n] in self._sessions:  # prefix taken by a colliding profile
                if n == len(full):
                    raise RuntimeError(f"cannot mint a unique id namespace for {key}")
                n += 1
            digest = self._digests[key] = full[:n]
        return digest

    def get_or_create_session(self, profile_dir: str | None = None) -> BrowserSessionManager:
        """Lazily build (and cache) the coordinator for ``profile_dir``.

        The session is handed its profile namespace so it can compose its own
        tabs' ids. Called from inside a tool coroutine, so an event loop is
        already running — safe for ``BrowserSessionManager.__init__`` to
        ``asyncio.create_task`` the reaper. Never invoked at import time. Runs
        synchronously on the single event loop (no await between lookup and
        insert), so get-then-set cannot interleave.
        """
        key = self.profile_key(profile_dir)
        digest = self.digest_for(key)
        session = self._sessions.get(digest)
        if session is None:
            logger.info(f"Initializing BrowserSessionManager (profile={key})")
            session = BrowserSessionManager(SeleniumChromeBackend(profile_dir=key), namespace=digest)
            self._sessions[digest] = session
            if not self._atexit_registered:
                atexit.register(self._shutdown)
                self._atexit_registered = True
        return session

    def _shutdown(self) -> None:
        """Close every profile's browser on interpreter exit.

        Registered exactly once (guarded by ``_atexit_registered``) the first
        time any session is created, so it fires a single time regardless of how
        many profiles are in play. The store is the only object with a view of
        all sessions, so teardown belongs here: without it the Chrome processes
        the store launched (and their SingletonLocks) outlive the server. Each
        close is best-effort — one profile failing to shut down must not strand
        the others.
        """
        logger.info("MCP Server shutting down")
        for session in self._sessions.values():
            try:
                session.close()
            except Exception as e:
                logger.warning(f"Error closing session during shutdown: {e}")

    def sessions(self) -> list[BrowserSessionManager]:
        """All live sessions, for aggregating tabs across profiles."""
        return list(self._sessions.values())

    def route(self, id: str) -> BrowserSessionManager:
        """Select the session that owns ``id`` (by its namespace prefix).

        Only the store has the global view needed to route; the chosen session
        owns the id from there — it splits off its own handle and composes its
        tabs' ids itself.
        """
        namespace = split_page_id(id)[0]
        session = self._sessions.get(namespace)
        if session is None:
            raise UnknownTabError(id)
        return session
