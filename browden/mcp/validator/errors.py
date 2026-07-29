class ValidationError(ValueError):
    """Raised when an MCP tool input fails validation."""


class SessionBusyError(RuntimeError):
    """Raised when a request can't get its browser session's driver lock in time.

    One profile is one Chrome, driven by one (non-thread-safe) WebDriver, so
    every driver op in a session is serialized behind that session's lock.
    Waiters queue; a waiter that is still queued after
    ``DRIVER_LOCK_TIMEOUT_SECONDS`` gives up with this rather than blocking its
    caller forever. A ``RuntimeError`` subclass so the tools that already fold
    session-level RuntimeErrors into an error envelope (``new_blank_tab``) keep
    doing the right thing.
    """

    def __init__(self, timeout_seconds: float):
        super().__init__(
            f"browser session busy — another request held it for longer than "
            f"{timeout_seconds:g}s; tabs in one profile are driven one at a time, so retry")
        self.timeout_seconds = timeout_seconds

    @property
    def envelope(self) -> dict:
        return {"error": str(self)}


def tab_gone_envelope(id: str) -> dict:
    """The standard error envelope returned when ``id`` no longer names an open tab.

    A tool that finds its tab has closed returns this (rather than raising) so the
    agent gets a clean, actionable message with the id echoed back. The one place
    the wording lives — server tools and the session manager both return it.
    """
    return {"error": f"tab {id} is no longer open — call list_tabs for current tabs", "id": id}
