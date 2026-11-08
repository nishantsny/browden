"""Unit-test fixtures shared across the suite.

The real Tranco snapshot is fetched by setup into the user config dir and is
never committed, so the unit tests can't read it. Instead we ship a tiny
committed fixture (``test/fixtures/tranco-mini.txt.gz``, the real top ~100
domains) and, for every unit test, repoint the Tranco default path at it. Tests
that build an allowlist from a dict or a fixture-less config fall through to
this default, so they see a small but real top-sites list (google.com #1,
cloudflare.com #2, …) with no network and full determinism.

The read gate also reduces a host to its registrable domain via the Public
Suffix List. The fresh PSL setup fetches is likewise not committed, so we pin
the PSL default at publicsuffix2's *bundled* snapshot — it covers every real TLD
(so mini-fixture domains reduce correctly) and needs no network. Tests that must
exercise a *fresh* PSL place their own ``public_suffix_list.dat`` next to a
snapshot and construct against that path.
"""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import browden.common.secret_portal as secret_portal
import browden.web_navigator.selenium_chrome.backend as selenium_backend
from browden.mcp.session_management import browser_session_store
from browden.mcp.validator import popularity, tranco

TRANCO_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tranco-mini.txt.gz"


@pytest.fixture(autouse=True)
def _tranco_fixture(monkeypatch):
    """Pin the default Tranco snapshot at the mini fixture and the default PSL at
    publicsuffix2's bundled list; clear the memoized loaders around each test.
    (``PopularityAllowlist.contains`` needs no clearing — its memo is per
    instance, and every test constructs its own instance.)"""
    monkeypatch.setattr(tranco, "DEFAULT_TRANCO_PATH", TRANCO_FIXTURE)
    monkeypatch.setattr(popularity, "DEFAULT_PSL_PATH", popularity._BUNDLED_PSL_PATH)
    tranco._load.cache_clear()
    popularity._psl.cache_clear()
    yield
    tranco._load.cache_clear()
    popularity._psl.cache_clear()


@pytest.fixture(autouse=True)
def _no_store_atexit(monkeypatch):
    """Keep unit-test session stores from registering real atexit hooks.

    ``BrowserSessionStore.get_or_create_session`` registers an interpreter-exit
    shutdown for the sessions it creates. In unit tests those sessions wrap fake
    backends — there is no Chrome to tear down — so the hooks only fire after
    pytest's summary and log "MCP Server shutting down" lines into the terminal.
    Stub the registration (in the store module's namespace only); the real
    atexit path is covered by the e2e suite, which drives real browsers.
    """
    monkeypatch.setattr(
        browser_session_store, "atexit",
        SimpleNamespace(register=lambda *a, **k: None))


@pytest.fixture(autouse=True)
def _no_host_dbus_probe(monkeypatch):
    """Keep Chrome's argv from being decided by the developer's own desktop.

    ``_chrome_args`` asks the host, over D-Bus, whether Chrome's password-store
    detection would hang on secret-portal activation, and names the store when it
    would (issue #182). The probe's one external dependency is the ``dbus-send``
    binary, so hiding that resolves the question to "cannot tell, change
    nothing" — deterministic everywhere, and it keeps the probe clear of the
    Chrome-launch fakes, which stub ``subprocess.Popen`` (what ``subprocess.run``
    is built on). The decision table itself is covered with an explicitly faked
    bus in ``test_password_store.py``.
    """
    real_which = shutil.which
    monkeypatch.setattr(
        secret_portal.shutil, "which",
        lambda cmd, *a, **k: None if cmd == "dbus-send" else real_which(cmd, *a, **k))
    # Host state, cached per process: clear it either side so no test inherits
    # another's answer.
    monkeypatch.setattr(selenium_backend, "_PASSWORD_STORE_CACHE", None)
    yield
    monkeypatch.setattr(selenium_backend, "_PASSWORD_STORE_CACHE", None)
