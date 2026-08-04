"""Reads/watches the system light-dark preference via the XDG Settings
portal (org.freedesktop.portal.Desktop), for the optional "follow system"
theme mode."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, SLOT, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface

logger = logging.getLogger(__name__)

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SETTINGS_IFACE = "org.freedesktop.portal.Settings"
APPEARANCE_NS = "org.freedesktop.appearance"
COLOR_SCHEME_KEY = "color-scheme"

NO_PREFERENCE, PREFER_DARK, PREFER_LIGHT = 0, 1, 2


class ThemePortal(QObject):
    """`colorSchemeChanged` fires with one of NO_PREFERENCE/PREFER_DARK/PREFER_LIGHT."""

    colorSchemeChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bus = QDBusConnection.sessionBus()
        self._iface = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, SETTINGS_IFACE, self._bus)
        self._available = self._iface.isValid()
        if self._available:
            self._bus.connect(
                PORTAL_SERVICE, PORTAL_PATH, SETTINGS_IFACE, "SettingChanged",
                self, SLOT("_onSettingChanged(QString,QString,QDBusVariant)"),
            )

    def available(self) -> bool:
        return self._available

    def read_color_scheme(self) -> int:
        if not self._available:
            return NO_PREFERENCE
        reply = self._iface.call("ReadOne", APPEARANCE_NS, COLOR_SCHEME_KEY)
        if reply.type() != reply.MessageType.ReplyMessage:
            return NO_PREFERENCE
        args = reply.arguments()
        if not args:
            return NO_PREFERENCE
        value = args[0]
        return value.variant() if hasattr(value, "variant") else value

    @Slot(str, str, 'QDBusVariant')
    def _onSettingChanged(self, namespace, key, value) -> None:
        if str(namespace) != APPEARANCE_NS or str(key) != COLOR_SCHEME_KEY:
            return
        try:
            self.colorSchemeChanged.emit(int(value.variant()))
        except Exception:
            logger.warning("Malformed SettingChanged payload for color-scheme", exc_info=True)
