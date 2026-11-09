"""Which password store browden names, and why (issue #182).

Chrome's password-store code path activates the freedesktop secret portal over
D-Bus; where that activation hangs, Chrome waits out a 25s reply timeout and the
first navigation of every browser launch waits with it. The cure —
``--password-store=basic`` — also re-keys the profile's cookie encryption, which
permanently drops every cookie already stored, so the decision is deliberately
narrow. These tests pin the decision rather than the timing: the stall is a
property of the host (a portal that cannot start) and cannot be reproduced on a
runner, so argv is the only thing that can be asserted everywhere.
"""
from unittest.mock import MagicMock

import pytest

import browden.common.secret_portal as secret_portal
import browden.web_navigator.selenium_chrome.backend as backend
from browden.common.secret_portal import PORTAL_BUS_NAME, SECRETS_BUS_NAME
from browden.web_navigator.selenium_chrome.backend import _chrome_args, _password_store_args

BASIC = "--password-store=basic"


@pytest.fixture(autouse=True)
def _clean_probe_state(monkeypatch):
    """The decision is cached per process; every test starts from unprobed."""
    monkeypatch.delenv("BROWDEN_PASSWORD_STORE", raising=False)
    monkeypatch.setattr(backend, "_PASSWORD_STORE_CACHE", None)
    yield


def _host(monkeypatch, *, bus="unix:path=/run/user/1000/bus",
          owned=None, activates=None):
    """Fake a host: which bus names are owned, and whether the portal starts."""
    owned = owned or {}
    monkeypatch.setattr(secret_portal, "session_bus_address", lambda: bus)
    monkeypatch.setattr(secret_portal, "bus_name_has_owner",
                        lambda name, timeout=None: owned.get(name))
    monkeypatch.setattr(secret_portal, "portal_activates",
                        lambda timeout=None: activates)


# --- the decision ----------------------------------------------------------

def test_no_session_bus_names_the_basic_store(monkeypatch):
    # Nothing to lose: with no bus, neither portal nor keyring is reachable and
    # Chrome lands on the basic store by itself. Naming it only stops it looking.
    _host(monkeypatch, bus=None)
    assert _password_store_args() == [BASIC]


def test_healthy_portal_leaves_the_store_alone(monkeypatch):
    # Portal already running: Chrome's detection is fast, so don't interfere.
    _host(monkeypatch, owned={PORTAL_BUS_NAME: True})
    assert _password_store_args() == []


def test_portal_that_activates_in_time_leaves_the_store_alone(monkeypatch):
    # Not currently owned but starts on demand within the probe budget — the
    # normal state of an idle desktop, and not a broken host.
    _host(monkeypatch, owned={PORTAL_BUS_NAME: False}, activates=True)
    assert _password_store_args() == []


def test_broken_portal_with_a_working_keyring_is_reported_not_overridden(monkeypatch):
    # The case on a session whose portal backend cannot run: the portal never
    # starts, so Chrome stalls, but it then uses the keyring successfully.
    # Forcing the basic store here would cure the stall by destroying the
    # profile's cookies — so browden warns and changes nothing.
    _host(monkeypatch, owned={PORTAL_BUS_NAME: False, SECRETS_BUS_NAME: True},
          activates=False)
    log = MagicMock()
    monkeypatch.setattr(backend, "logger", log)

    assert _password_store_args() == []

    log.warning.assert_called_once()
    said = log.warning.call_args[0][0]
    assert "25s" in said                             # names the cost
    assert "BROWDEN_PASSWORD_STORE=basic" in said    # names the opt-in
    assert "#182" in said                            # names the issue
    assert "portals.conf" in said                    # names the actual remedy


def test_broken_portal_and_no_keyring_names_the_basic_store(monkeypatch):
    # Chrome would stall and then fall back to basic regardless, so skip the stall.
    _host(monkeypatch, owned={PORTAL_BUS_NAME: False, SECRETS_BUS_NAME: False},
          activates=False)
    assert _password_store_args() == [BASIC]


def test_unanswerable_probe_leaves_the_store_alone(monkeypatch):
    # No dbus-send to ask with: "unknown" must not be read as "broken", because
    # guessing wrong costs the profile's cookies.
    _host(monkeypatch, owned={PORTAL_BUS_NAME: None}, activates=None)
    assert _password_store_args() == []


def test_decision_is_probed_once_per_process(monkeypatch):
    # The probe blocks for up to PROBE_TIMEOUT_SECONDS on a broken host; paying
    # that per browser launch would hand back the latency we just saved.
    calls = []
    _host(monkeypatch, bus=None)
    real = backend._probe_password_store

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(backend, "_probe_password_store", counted)
    assert _password_store_args() == [BASIC]
    assert _password_store_args() == [BASIC]
    assert len(calls) == 1


# --- the operator override -------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("basic", ["--password-store=basic"]),
    ("gnome-libsecret", ["--password-store=gnome-libsecret"]),
    ("kwallet5", ["--password-store=kwallet5"]),
    ("detect", []),          # force Chrome's own default
    ("DETECT", []),
])
def test_env_override_wins(monkeypatch, value, expected):
    _host(monkeypatch, owned={PORTAL_BUS_NAME: True})     # would say "leave alone"
    monkeypatch.setenv("BROWDEN_PASSWORD_STORE", value)
    assert _password_store_args() == expected


def test_env_override_is_not_cached_away(monkeypatch):
    # The override is read live, so it still applies after a probe has been cached.
    _host(monkeypatch, bus=None)
    assert _password_store_args() == [BASIC]
    monkeypatch.setenv("BROWDEN_PASSWORD_STORE", "detect")
    assert _password_store_args() == []


# --- the bus probe itself --------------------------------------------------

def test_name_has_owner_reads_the_reply(monkeypatch):
    monkeypatch.setattr(secret_portal.shutil, "which", lambda b: "/usr/bin/dbus-send")
    monkeypatch.setattr(secret_portal.subprocess, "run",
                        lambda *a, **k: MagicMock(returncode=0, stdout="   boolean true\n"))
    assert secret_portal.bus_name_has_owner("org.example") is True

    monkeypatch.setattr(secret_portal.subprocess, "run",
                        lambda *a, **k: MagicMock(returncode=0, stdout="   boolean false\n"))
    assert secret_portal.bus_name_has_owner("org.example") is False


def test_name_has_owner_is_unknown_without_dbus_send(monkeypatch):
    monkeypatch.setattr(secret_portal.shutil, "which", lambda b: None)
    assert secret_portal.bus_name_has_owner("org.example") is None
    assert secret_portal.portal_activates() is None


def test_name_has_owner_never_requests_activation(monkeypatch):
    # NameHasOwner is answered by the bus daemon; StartServiceByName is what
    # blocks. If this probe ever asked for activation it would hang exactly where
    # it is meant to detect a hang.
    seen = {}
    monkeypatch.setattr(secret_portal.shutil, "which", lambda b: "/usr/bin/dbus-send")

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return MagicMock(returncode=0, stdout="boolean true")

    monkeypatch.setattr(secret_portal.subprocess, "run", fake_run)
    secret_portal.bus_name_has_owner("org.example")
    assert "org.freedesktop.DBus.NameHasOwner" in seen["argv"]
    assert not any("StartServiceByName" in a for a in seen["argv"])


def test_portal_activation_timeout_means_broken(monkeypatch):
    monkeypatch.setattr(secret_portal.shutil, "which", lambda b: "/usr/bin/dbus-send")

    def boom(*a, **k):
        raise secret_portal.subprocess.TimeoutExpired(cmd="dbus-send", timeout=1.0)

    monkeypatch.setattr(secret_portal.subprocess, "run", boom)
    assert secret_portal.portal_activates() is False


def test_portal_probe_is_bounded_by_our_own_timeout(monkeypatch):
    # The whole point: Chrome's 25s wait is not ours to shorten, so we must cap
    # the question we ask instead.
    seen = {}
    monkeypatch.setattr(secret_portal.shutil, "which", lambda b: "/usr/bin/dbus-send")

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr(secret_portal.subprocess, "run", fake_run)
    secret_portal.portal_activates(1.0)
    assert "--reply-timeout=1000" in seen["argv"]
    assert seen["timeout"] is not None and seen["timeout"] <= 1.0 + 2


def test_session_bus_falls_back_to_the_per_uid_socket(monkeypatch):
    # A user service often has no DBUS_SESSION_BUS_ADDRESS in its environment,
    # so the well-known socket is the fallback. Asserted as a literal string: a
    # path object would render this with backslashes on Windows.
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(secret_portal.os, "getuid", lambda: 4242, raising=False)
    monkeypatch.setattr(secret_portal.os.path, "exists", lambda p: True)
    assert secret_portal.session_bus_address() == "unix:path=/run/user/4242/bus"


def test_session_bus_is_absent_without_a_posix_uid(monkeypatch):
    # Windows has no /run/user/<uid>/bus; probing for one must not crash.
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delattr(secret_portal.os, "getuid", raising=False)
    assert secret_portal.session_bus_address() is None


# --- the install-time diagnosis -------------------------------------------

def test_diagnose_is_silent_on_a_healthy_host(monkeypatch):
    _host(monkeypatch, owned={PORTAL_BUS_NAME: True})
    assert secret_portal.diagnose() is None


def test_diagnose_is_silent_without_a_bus(monkeypatch):
    # A container has no portal to hang on, so there is nothing to warn about.
    _host(monkeypatch, bus=None)
    assert secret_portal.diagnose() is None


def test_diagnose_is_silent_when_it_cannot_tell(monkeypatch):
    _host(monkeypatch, owned={PORTAL_BUS_NAME: None}, activates=None)
    assert secret_portal.diagnose() is None


def test_diagnose_warns_and_names_the_cookie_risk(monkeypatch):
    _host(monkeypatch, owned={PORTAL_BUS_NAME: False, SECRETS_BUS_NAME: True},
          activates=False)
    said = secret_portal.diagnose()
    assert said is not None
    assert "25s" in said
    assert "portals.conf" in said
    assert "cookie" in said           # the operator must hear the trade


def test_diagnose_warns_without_the_cookie_risk_when_there_is_no_keyring(monkeypatch):
    _host(monkeypatch, owned={PORTAL_BUS_NAME: False, SECRETS_BUS_NAME: False},
          activates=False)
    said = secret_portal.diagnose()
    assert said is not None
    assert "--password-store=basic" in said


# --- the argv the backend actually launches --------------------------------

def test_chrome_args_carry_the_decision(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWDEN_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.delenv("BROWDEN_HEADLESS", raising=False)
    _host(monkeypatch, bus=None)
    assert BASIC in _chrome_args(tmp_path / "profile", 9222)


def test_chrome_args_omit_the_flag_on_a_healthy_host(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWDEN_CHROME_BINARY", "/usr/bin/google-chrome")
    monkeypatch.delenv("BROWDEN_HEADLESS", raising=False)
    _host(monkeypatch, owned={PORTAL_BUS_NAME: True})
    args = _chrome_args(tmp_path / "profile", 9222)
    assert not any(a.startswith("--password-store") for a in args)
