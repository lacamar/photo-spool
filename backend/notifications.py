"""Desktop notifications via org.freedesktop.Notifications.

Sending uses `dbus` (dbus-python) rather than QtDBus: Notify()'s
`replaces_id` parameter is strictly typed as D-Bus UINT32, and PySide6's
QtDBus bindings (as of 6.11, no `QVariant` class exposed to Python) have no
way to marshal a plain Python int as anything but INT32 -- which the
notification daemon on this system (and any spec-conformant one) rejects
outright. Receiving the ActionInvoked signal back into the Qt event loop
works fine through QtDBus, so that direction stays fully native with no
extra dependency.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, SLOT, Signal, Slot
from PySide6.QtDBus import QDBusConnection

try:
    import dbus
except ImportError:  # pragma: no cover - dbus-python always present via dnf dep
    dbus = None

logger = logging.getLogger(__name__)

NOTIFY_SERVICE = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"
NOTIFY_IFACE = "org.freedesktop.Notifications"


class NotificationManager(QObject):
    """Sends desktop notifications and reports which session (if any) an
    "Open" action referred to, via `openRequested`."""

    openRequested = Signal(int)  # session_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._session_by_notif_id: dict[int, int] = {}
        self._session_bus = None
        if dbus is not None:
            try:
                self._session_bus = dbus.SessionBus()
            except Exception:
                logger.warning("Could not connect to session bus for notifications", exc_info=True)
        qbus = QDBusConnection.sessionBus()
        qbus.connect(
            "", NOTIFY_PATH, NOTIFY_IFACE, "ActionInvoked",
            self, SLOT("_onActionInvoked(uint,QString)"),
        )
        qbus.connect(
            "", NOTIFY_PATH, NOTIFY_IFACE, "NotificationClosed",
            self, SLOT("_onNotificationClosed(uint,uint)"),
        )

    def available(self) -> bool:
        return self._session_bus is not None

    def send(self, summary: str, body: str, session_id: int | None = None, transient: bool = False) -> int:
        """Fire-and-forget; returns the D-Bus notification id, or 0 on
        failure (never raises -- notifications are an enhancement, not a
        dependency of core functionality). `transient=True` sets the
        freedesktop-spec "transient" hint, telling the notification daemon
        itself not to keep this one in its own persistent history panel."""
        if self._session_bus is None:
            return 0
        try:
            proxy = self._session_bus.get_object(NOTIFY_SERVICE, NOTIFY_PATH)
            iface = dbus.Interface(proxy, NOTIFY_IFACE)
            actions = ["open", "Open"] if session_id is not None else []
            hints = {"transient": dbus.Boolean(True, variant_level=1)} if transient else {}
            notif_id = int(iface.Notify(
                "Photo Spool", dbus.UInt32(0), "", summary, body, actions, hints, -1,
            ))
        except Exception:
            logger.warning("Failed to send desktop notification", exc_info=True)
            return 0
        if session_id is not None:
            self._session_by_notif_id[notif_id] = session_id
        return notif_id

    @Slot('uint', 'QString')
    def _onActionInvoked(self, notif_id, action_key) -> None:
        if str(action_key) != "open":
            return
        session_id = self._session_by_notif_id.get(int(notif_id))
        if session_id is not None:
            self.openRequested.emit(session_id)

    @Slot('uint', 'uint')
    def _onNotificationClosed(self, notif_id, reason) -> None:
        self._session_by_notif_id.pop(int(notif_id), None)
