"""Is the D-Bus secret portal healthy on this host?

Chrome's password-store code path asks D-Bus to activate the freedesktop secret
portal (``org.freedesktop.portal.Desktop``). Where that activation never
completes, Chrome waits out D-Bus's 25s default reply timeout, and the **first
navigation of every browser launch** waits with it -- measured at 25.1s to load
a page on 127.0.0.1 whose TTFB was 4ms. See issue #182.

The usual cause is an ``xdg-desktop-portal`` that cannot finish starting: its
configuration prefers a backend that is not usable on the host (a GNOME backend
on a session that is not GNOME, say), so each attempt times out and the service
is left failed. Nothing then owns the name, every activation request hangs, and
the stall is paid once per browser launch.

Chrome's own timeout is not ours to shorten, so browden asks the same question
first, with a bound of its own. This module is the probe; the policy that acts on
it lives in the selenium backend, and ``onetime_setup`` reports it at install
time. Kept free of third-party imports so setup can run it before any
dependency is installed.
"""
import os
import shutil
import subprocess
from pathlib import Path

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
SECRETS_BUS_NAME = "org.freedesktop.secrets"

# A healthy portal is a small, service-activated binary: it answers in tens to a
# few hundred ms. One second is comfortably above that and 25x below the stall
# being detected, so a host that is merely loaded is not misread as broken.
PROBE_TIMEOUT_SECONDS = 1.0

REMEDY = (
    "Repair the host's portal: `systemctl --user status xdg-desktop-portal.service` "
    "shows why it failed. If its configuration prefers a backend that cannot run "
    "here, name a usable one in ~/.config/xdg-desktop-portal/portals.conf "
    "(e.g. `default=gtk;`, plus `<interface>=none;` for the interfaces that "
    "backend does not implement, or they fall back to the unusable one and time "
    "out again)."
)


def session_bus_address() -> str | None:
    """The D-Bus session bus address, or None if this host has no session bus.

    Honours ``DBUS_SESSION_BUS_ADDRESS`` and falls back to the well-known per-uid
    socket, which is where a user service finds the bus when the variable was
    never exported into its environment.
    """
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").strip()
    if addr:
        return addr
    try:
        sock = Path(f"/run/user/{os.getuid()}/bus")
    except AttributeError:          # no getuid on this platform
        return None
    return f"unix:path={sock}" if sock.exists() else None


def bus_name_has_owner(name: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool | None:
    """Is ``name`` currently owned on the session bus?

    Asks the bus daemon, which is always running and answers without activating
    anything — so this can never incur the activation stall it exists to detect.
    Returns None when the question could not be put (no ``dbus-send`` binary, the
    call failed), which callers must treat as "unknown", not as "no".
    """
    dbus_send = shutil.which("dbus-send")
    if dbus_send is None:
        return None
    try:
        proc = subprocess.run(
            [dbus_send, "--session", "--print-reply",
             f"--reply-timeout={int(timeout * 1000)}",
             "--dest=org.freedesktop.DBus", "/", "org.freedesktop.DBus.NameHasOwner",
             f"string:{name}"],
            capture_output=True, text=True, timeout=timeout + 2)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return "boolean true" in proc.stdout


def portal_activates(timeout: float = PROBE_TIMEOUT_SECONDS) -> bool | None:
    """Can the secret portal be started within ``timeout``?

    This is the question Chrome blocks on, asked with a bound. Returns None if it
    could not be asked at all.
    """
    dbus_send = shutil.which("dbus-send")
    if dbus_send is None:
        return None
    try:
        proc = subprocess.run(
            [dbus_send, "--session", "--print-reply",
             f"--reply-timeout={int(timeout * 1000)}",
             "--dest=org.freedesktop.DBus", "/", "org.freedesktop.DBus.StartServiceByName",
             f"string:{PORTAL_BUS_NAME}", "uint32:0"],
            capture_output=True, text=True, timeout=timeout + 2)
    except subprocess.TimeoutExpired:
        return False
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def portal_is_reachable(timeout: float = PROBE_TIMEOUT_SECONDS) -> bool | None:
    """True / False / None (could not tell), cheapest question first."""
    if bus_name_has_owner(PORTAL_BUS_NAME, timeout) is True:
        return True
    activates = portal_activates(timeout)
    return None if activates is None else activates


def diagnose(timeout: float = PROBE_TIMEOUT_SECONDS) -> str | None:
    """A one-paragraph warning if this host will stall, else None.

    Used by setup to tell an operator at install time, on whatever machine they
    are installing on, rather than leaving them to discover a 25s first
    navigation later and blame browden for it.
    """
    if session_bus_address() is None:
        return None             # no bus: no portal to hang on, nothing to warn about
    if portal_is_reachable(timeout) is not False:
        return None             # healthy, or unanswerable — do not cry wolf
    detail = (
        "Chrome's first navigation after each browser launch will stall ~25s on "
        f"this host: the D-Bus secret portal ({PORTAL_BUS_NAME}) cannot be "
        "activated, and Chrome waits out D-Bus's default reply timeout. "
        f"{REMEDY}"
    )
    if bus_name_has_owner(SECRETS_BUS_NAME, timeout) is True:
        return detail + (
            " A secret service IS available here, so browden will not touch the "
            "password store: forcing BROWDEN_PASSWORD_STORE=basic would remove the "
            "stall but re-key the profile, and Chrome permanently drops every "
            "cookie it can then no longer decrypt — a one-way logout from every "
            "logged-in site."
        )
    return detail + (
        " No secret service is available either, so browden will pass "
        "--password-store=basic and skip the stall; Chrome would end up on that "
        "store anyway."
    )
