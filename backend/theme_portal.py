"""System light/dark preference and accent colour via the XDG Settings
portal, plus an optional $XDG_CONFIG_HOME/photo-spool/theme.json palette
override (same keys as tidings': bg/side/raised/fg/sub/line/accent/opacity,
e.g. written by a noctalia template)."""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import Property, QFileSystemWatcher, QObject, SLOT, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface
from PySide6.QtGui import QColor

from . import paths

logger = logging.getLogger(__name__)

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SETTINGS_IFACE = "org.freedesktop.portal.Settings"
APPEARANCE_NS = "org.freedesktop.appearance"
COLOR_SCHEME_KEY = "color-scheme"
ACCENT_KEY = "accent-color"

NO_PREFERENCE, PREFER_DARK, PREFER_LIGHT = 0, 1, 2


def _variant(value):
    return value.variant() if hasattr(value, "variant") else value


def _accent_name(rgb) -> str:
    try:
        r, g, b = (float(c) for c in rgb)
    except (TypeError, ValueError):
        return ""
    return QColor.fromRgbF(r, g, b).name() if all(0 <= c <= 1 for c in (r, g, b)) else ""


class ThemePortal(QObject):
    """`colorSchemeChanged` fires with one of NO_PREFERENCE/PREFER_DARK/PREFER_LIGHT."""

    colorSchemeChanged = Signal(int)
    paletteChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accent = ""
        self._custom: dict = {}
        self._bus = QDBusConnection.sessionBus()
        self._iface = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, SETTINGS_IFACE, self._bus)
        self._available = self._iface.isValid()
        if self._available:
            self._bus.connect(
                PORTAL_SERVICE, PORTAL_PATH, SETTINGS_IFACE, "SettingChanged",
                self, SLOT("_onSettingChanged(QString,QString,QDBusVariant)"),
            )
            self._accent = _accent_name(self._read(ACCENT_KEY))

        self._theme_file = paths.theme_file()
        self._theme_file.parent.mkdir(parents=True, exist_ok=True)
        # generators either replace the file (dir event) or rewrite it in place (file event)
        self._watcher = QFileSystemWatcher([str(self._theme_file.parent)], self)
        self._watcher.directoryChanged.connect(self._load_custom)
        self._watcher.fileChanged.connect(self._load_custom)
        self._load_custom()

    def available(self) -> bool:
        return self._available

    def _read(self, key: str):
        reply = self._iface.call("ReadOne", APPEARANCE_NS, key)
        if reply.type() != reply.MessageType.ReplyMessage or not reply.arguments():
            return None
        return _variant(reply.arguments()[0])

    def read_color_scheme(self) -> int:
        if not self._available:
            return NO_PREFERENCE
        value = self._read(COLOR_SCHEME_KEY)
        return NO_PREFERENCE if value is None else value

    def _load_custom(self, *_):
        path = str(self._theme_file)
        if self._theme_file.exists() and path not in self._watcher.files():
            self._watcher.addPath(path)
        try:
            self._custom = json.loads(self._theme_file.read_text())
        except (OSError, ValueError):
            self._custom = {}
        self.paletteChanged.emit()

    @Slot(str, str, 'QDBusVariant')
    def _onSettingChanged(self, namespace, key, value) -> None:
        if str(namespace) != APPEARANCE_NS:
            return
        try:
            if str(key) == COLOR_SCHEME_KEY:
                self.colorSchemeChanged.emit(int(value.variant()))
            elif str(key) == ACCENT_KEY:
                self._accent = _accent_name(value.variant())
                self.paletteChanged.emit()
        except Exception:
            logger.warning("Malformed SettingChanged payload for %s", key, exc_info=True)

    accent = Property(str, lambda self: self._accent, notify=paletteChanged)
    custom = Property("QVariant", lambda self: self._custom, notify=paletteChanged)
