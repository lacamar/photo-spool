#!/usr/bin/env python3
"""Entry point for the Photo Import desktop app."""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
# Qt's QML disk bytecode cache (~/.cache/photo-import/.../qmlcache) is meant
# to auto-invalidate when a .qml file's mtime/size changes, but that's
# fragile across reinstalls (e.g. RPM upgrades, tarballs from `git
# archive`) -- a stale cache silently keeps serving old compiled QML even
# though the source on disk is current. This app is small enough that
# recompiling QML from source on every launch is not measurably slower,
# so just always do that.
os.environ.setdefault("QML_DISABLE_DISK_CACHE", "1")

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonType
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from backend.app_controller import AppController
from backend.thumbnail_provider import ThumbnailImageProvider

# Arbitrary but fixed name for the single-instance IPC socket -- see
# _acquire_single_instance below. Bumping this would let two instances run
# again until both are on a build with the new name, so there's no reason
# to ever change it.
SINGLE_INSTANCE_KEY = "photo-import-single-instance"


def _acquire_single_instance(app: QApplication) -> QLocalServer | None:
    """Returns a listening QLocalServer if this is the only running
    instance. If another instance is already listening, nudges it to raise
    its window and returns None -- the caller should exit immediately
    without touching the DB or starting any backend workers.

    This matters more than the usual "annoying to have two windows" case:
    AppController starts a DeviceWatcher that auto-imports from any
    inserted card. Two independent instances each detecting the same card
    as newly found (neither aware of the other) raced to import the same
    photos and produced "(2)"/"(3)"/"(4)" duplicate files before this
    existed -- see the 0.2.8 changelog entry.
    """
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_KEY)
    if socket.waitForConnected(200):
        socket.write(b"show")
        socket.waitForBytesWritten(200)
        socket.disconnectFromServer()
        return None

    # No live instance -- clean up a stale socket file a crashed previous
    # instance may have left behind before claiming the name ourselves.
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer(app)
    server.listen(SINGLE_INSTANCE_KEY)
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Photo Import")
    parser.add_argument("--demo", action="store_true", help="Seed demo session history if the database is empty")
    args = parser.parse_args()

    # QApplication (not just QGuiApplication) so QtQuick.Dialogs' native
    # folder dialog has QtWidgets available if the platform theme needs it.
    app = QApplication(sys.argv)
    app.setApplicationName("Photo Import")
    app.setOrganizationName("photo-import")
    # The window's close button hides it instead of closing (see
    # Main.qml's onClosing) so the app keeps importing in the background;
    # this is the belt-and-suspenders match on the Qt side so a hidden
    # window is never mistaken for "last window closed".
    app.setQuitOnLastWindowClosed(False)
    # Launched via `python3 main.py`, Qt's Wayland platform would otherwise
    # derive the toplevel app_id from the interpreter binary ("python3"),
    # breaking icon/window-list matching against photo-import.desktop.
    app.setDesktopFileName("photo-import")

    instance_server = _acquire_single_instance(app)
    if instance_server is None:
        return 0

    icon_path = APP_ROOT / "icons" / "photo-import.svg"
    app.setWindowIcon(QIcon(str(icon_path)) if icon_path.exists() else QIcon.fromTheme("photo-import"))

    qml_dir = APP_ROOT / "qml"
    theme_url = QUrl.fromLocalFile(str(qml_dir / "Theme.qml"))
    qmlRegisterSingletonType(theme_url, "PhotoImport", 1, 0, "Theme")

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(qml_dir))
    engine.addImageProvider("thumb", ThumbnailImageProvider())

    controller = AppController(app, demo=args.demo)
    context = engine.rootContext()
    context.setContextProperty("appController", controller)
    context.setContextProperty("sessionModel", controller.sessionModel)
    context.setContextProperty("notificationModel", controller.notificationModel)
    context.setContextProperty("sourcesModel", controller.sourcesModel)

    app.aboutToQuit.connect(controller.shutdown)

    engine.load(QUrl.fromLocalFile(str(qml_dir / "Main.qml")))
    if not engine.rootObjects():
        # aboutToQuit never fires if we never reach the event loop, so stop
        # background threads directly -- otherwise Python destroys a
        # still-running QThread at interpreter exit, which Qt treats as fatal.
        controller.shutdown()
        return -1

    window = engine.rootObjects()[0]

    def show_window():
        window.show()
        window.raise_()
        window.requestActivate()

    # A second launch connects here (see _acquire_single_instance) instead
    # of starting its own instance -- treat that exactly like a tray click.
    # The message content doesn't matter (only "show" is ever sent); any
    # incoming connection at all means "someone tried to launch me again".
    def _on_instance_connection():
        conn = instance_server.nextPendingConnection()
        show_window()
        if conn is not None:
            conn.disconnectFromServer()

    instance_server.newConnection.connect(_on_instance_connection)

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(app.windowIcon(), app)
        tray.setToolTip("Photo Import")

        tray_menu = QMenu()
        show_action = QAction("Open Photo Import", tray_menu)
        show_action.triggered.connect(show_window)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        quit_action = QAction("Quit", tray_menu)
        quit_action.triggered.connect(app.quit)
        tray_menu.addAction(quit_action)
        tray.setContextMenu(tray_menu)

        def _on_tray_activated(reason):
            # Trigger is a plain left-click (the cross-platform "primary
            # activation" reason) -- checks for new importable media
            # without needing to open the window at all; opening the
            # window is now exclusively a right-click menu action (see
            # show_action above), so a click never fights with the menu.
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                controller.refreshDevices()
                tray.showMessage(
                    "Photo Import", "Checked for new photos.",
                    QSystemTrayIcon.MessageIcon.Information, 2500,
                )

        tray.activated.connect(_on_tray_activated)
        tray.show()

    # Route SIGINT/SIGTERM through app.quit() so aboutToQuit (and the worker
    # thread joins in AppController.shutdown) always runs, instead of Qt's
    # C++ objects getting torn down mid-flight at abrupt interpreter exit.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal_wakeup = QTimer()
    signal_wakeup.timeout.connect(lambda: None)
    signal_wakeup.start(200)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
