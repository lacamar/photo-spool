"""Suspend/idle inhibition for the duration of an active import, via the
desktop-portal Inhibit interface (org.freedesktop.portal.Inhibit) -- works
across GNOME/KDE/etc, not tied to any one compositor or session manager.
Real-world motivation: a large card's import is a genuinely multi-minute
operation (see import_worker.py's own phase-progress comments), and nothing
before this stopped the system from suspending mid-import on a laptop.

Plain dbus-python (not QtDBus), matching notifications.py's precedent --
kept deliberately free of any PySide6 import so this stays trivially unit-
testable (see tests/test_inhibit.py) without touching a real session bus,
unlike the QObject-based backend modules that connect to D-Bus eagerly in
their constructor.

Best-effort throughout: a minimal Wayland setup with no xdg-desktop-portal
Inhibit implementation just means imports proceed without inhibition, same
as before this existed -- never worth failing or blocking an import over."""
from __future__ import annotations

import logging

try:
    import dbus
except ImportError:  # pragma: no cover - dbus-python always present via dnf dep
    dbus = None

logger = logging.getLogger(__name__)

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
INHIBIT_INTERFACE = "org.freedesktop.portal.Inhibit"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

# Bitmask per the portal spec: logout=1, user-switch=2, suspend=4, idle=8.
# Only suspend+idle are relevant here -- an import shouldn't block the user
# from logging out or switching sessions, just from the machine going to
# sleep or the screen locking mid-transfer.
FLAG_SUSPEND = 4
FLAG_IDLE = 8


class ImportInhibitor:
    """Call begin() whenever import work becomes active and end() when it's
    no longer needed. Reference-counted (not just a bool) since
    ImportWorker can have several sessions queued/running back to back --
    only the *first* begin() actually calls the portal, and only the
    matching final end() releases it."""

    def __init__(self):
        self._count = 0
        self._request_path: str | None = None

    def begin(self) -> None:
        self._count += 1
        if self._count > 1 or self._request_path is not None or dbus is None:
            return
        try:
            bus = dbus.SessionBus()
            portal = bus.get_object(PORTAL_BUS_NAME, PORTAL_OBJECT_PATH)
            handle = portal.Inhibit(
                "", dbus.UInt32(FLAG_SUSPEND | FLAG_IDLE), {"reason": "Importing photos"},
                dbus_interface=INHIBIT_INTERFACE,
            )
            self._request_path = str(handle)
        except Exception:
            logger.info("Could not inhibit idle/suspend via portal", exc_info=True)
            self._request_path = None

    def end(self) -> None:
        self._count = max(self._count - 1, 0)
        if self._count > 0 or self._request_path is None or dbus is None:
            return
        try:
            bus = dbus.SessionBus()
            request = bus.get_object(PORTAL_BUS_NAME, self._request_path)
            request.Close(dbus_interface=REQUEST_INTERFACE)
        except Exception:
            logger.info("Could not release idle/suspend inhibit", exc_info=True)
        finally:
            self._request_path = None
