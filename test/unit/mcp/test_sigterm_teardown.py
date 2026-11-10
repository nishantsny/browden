"""SIGTERM must unwind, so the browser teardown runs (issue #183).

Chrome teardown hangs off ``BrowserSessionStore._shutdown``, registered with
``atexit``. atexit does not run when a process dies from a signal's default
disposition, so a plain ``kill`` orphaned every Chrome the server had launched —
each still holding its profile's ``SingletonLock`` — while Ctrl-C cleaned up
fine, because KeyboardInterrupt unwinds. These tests pin the handler that closes
that gap; the e2e suite covers the reaping itself with a real browser.
"""
import signal

import pytest

from browden.mcp.server import _install_sigterm_handler


@pytest.fixture(autouse=True)
def _restore_sigterm():
    """Signal handlers are process-global and monkeypatch cannot undo them.

    Holds its own reference to ``signal.signal``: this fixture is torn down
    while a test's monkeypatching of that very function is still in effect.
    """
    install = signal.signal
    previous = signal.getsignal(signal.SIGTERM)
    yield
    install(signal.SIGTERM, previous)


def test_a_handler_is_installed_for_sigterm():
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    _install_sigterm_handler()

    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), "default disposition kills the process without running atexit"
    assert handler not in (signal.SIG_DFL, signal.SIG_IGN)


def test_the_handler_raises_system_exit_rather_than_tearing_down_itself():
    # It runs on the main thread between bytecodes, holding whatever locks that
    # thread already held, so it must stay trivial and let the unwind do the work.
    _install_sigterm_handler()
    handler = signal.getsignal(signal.SIGTERM)

    with pytest.raises(SystemExit) as caught:
        handler(signal.SIGTERM, None)
    # 128+n is the shell's convention for "died on signal n", so the exit status
    # still says what happened.
    assert caught.value.code == 128 + signal.SIGTERM


def test_sigint_is_left_to_python():
    # KeyboardInterrupt already unwinds and runs atexit; replacing it would only
    # risk breaking a path that works.
    before = signal.getsignal(signal.SIGINT)
    _install_sigterm_handler()
    assert signal.getsignal(signal.SIGINT) is before


def test_install_survives_a_non_main_thread(monkeypatch):
    # signal.signal raises ValueError off the main thread (an embedded host).
    # Serving must still start; it just cannot promise a clean signal shutdown.
    def refuse(*_a, **_k):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", refuse)
    _install_sigterm_handler()          # must not raise
