"""QObject facade exposed to QML as `appController`. Owns the DB connection
(GUI thread only) and orchestrates every backend module."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from . import db, device_watch, dnglab_setup, notifications, paths, settings_store
from .blur import compositor_supports_blur
from .import_worker import ImportRequest, ImportWorker
from .list_models import NotificationListModel, SessionListModel
from .notifications import NotificationManager
from .theme_portal import PREFER_DARK, ThemePortal

logger = logging.getLogger(__name__)


class AppController(QObject):
    systemPrefersDarkChanged = Signal()
    unreadNotificationCountChanged = Signal()
    dnglabReadyChanged = Signal()
    watchEnabledChanged = Signal()
    navigateToSession = Signal(int, str)  # session_id, device_label
    toast = Signal(str)

    def __init__(self, app, demo: bool = False, parent=None):
        super().__init__(parent)
        self._app = app
        self._conn = db.connect()
        if demo:
            from . import demo as demo_module
            demo_module.seed_if_empty(self._conn)

        self.sessionModel = SessionListModel(self)
        self.notificationModel = NotificationListModel(self)
        self.sessionModel.load(self._conn)
        self.notificationModel.load(self._conn)

        self._notification_manager = NotificationManager(self)
        self._notification_manager.openRequested.connect(self._on_notification_open)
        self._theme_portal = ThemePortal(self)
        self._system_prefers_dark = self._theme_portal.read_color_scheme() == PREFER_DARK
        self._theme_portal.colorSchemeChanged.connect(self._on_system_color_scheme_changed)

        self._blur_available = compositor_supports_blur()

        self._dnglab_ready = dnglab_setup.find_existing() is not None
        self._dnglab_worker: dnglab_setup.EnsureWorker | None = None
        if not self._dnglab_ready:
            self._start_dnglab_setup()

        self._import_worker = ImportWorker(self)
        self._import_worker.sessionStarted.connect(self._on_session_started)
        self._import_worker.sessionProgress.connect(self._on_session_progress)
        self._import_worker.sessionFinished.connect(self._on_session_finished)
        self._import_worker.dnglabUnavailable.connect(self._on_dnglab_unavailable)
        self._import_worker.start()

        self._device_watcher = device_watch.DeviceWatcher(self)
        self._device_watcher.sourceFound.connect(self._on_source_found)
        settings = settings_store.all_settings(self._conn)
        self._watch_enabled = bool(settings.get("watch_enabled", True))
        if self._watch_enabled:
            self._device_watcher.start(mtp_enabled=bool(settings.get("mtp_enabled", True)))

    def shutdown(self) -> None:
        self._device_watcher.stop()
        self._import_worker.request_stop()
        self._import_worker.wait(5000)
        if self._dnglab_worker is not None:
            self._dnglab_worker.wait(1000)
        self._conn.close()

    # --- device detection / import sessions --------------------------------------

    def _on_source_found(self, root: str, label: str, kind: str) -> None:
        text = f"{label}: importing new photos…"
        notifications.record(self._conn, text, None, "device_detected")
        self._notification_manager.send("Photo Import", text)
        self.notificationModel.load(self._conn)
        self.unreadNotificationCountChanged.emit()
        self._import_worker.submit(ImportRequest(source_root=root, device_label=label, kind=kind))

    def _on_session_started(self, session_id: int) -> None:
        self.sessionModel.upsert(session_id, self._conn)

    def _on_session_progress(self, session_id: int, done: int, total: int, filename: str) -> None:
        self.sessionModel.set_progress(session_id, done, total, filename)

    def _on_session_finished(self, session_id: int) -> None:
        self.sessionModel.upsert(session_id, self._conn)
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return
        if row["status"] == "failed":
            text = f"{row['device_label']}: import failed -- {row['error_message'] or 'unknown error'}"
            kind = "error"
        elif row["imported_count"] or row["duplicate_count"]:
            parts = [f"{row['imported_count']} imported"]
            if row["duplicate_count"]:
                parts.append(f"{row['duplicate_count']} already had copies")
            if row["failed_count"]:
                parts.append(f"{row['failed_count']} failed")
            text = f"{row['device_label']}: " + ", ".join(parts) + "."
            kind = "import_complete"
        else:
            return  # nothing found on the card -- not worth a notification
        notifications.record(self._conn, text, session_id, kind)
        if settings_store.get(self._conn, "notify_on_complete"):
            self._notification_manager.send("Photo Import", text, session_id)
        self.notificationModel.load(self._conn)
        self.unreadNotificationCountChanged.emit()

    def _on_dnglab_unavailable(self, session_id: int) -> None:
        self.toast.emit("The DNG converter isn't available -- check your network connection.")

    @Slot(int, result='QVariantList')
    def getSessionFiles(self, session_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM session_files WHERE session_id = ? ORDER BY sort_order", (session_id,),
        ).fetchall()
        return [
            {
                "id": r["id"], "sourceFilename": r["source_filename"], "status": r["status"],
                "destPath": r["dest_path"], "errorMessage": r["error_message"],
            }
            for r in rows
        ]

    @Slot(int)
    def ejectSession(self, session_id: int) -> None:
        row = self._conn.execute(
            "SELECT ejectable_path FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or not row["ejectable_path"]:
            return
        ok = self._device_watcher.eject(row["ejectable_path"])
        if ok:
            with self._conn:
                self._conn.execute("UPDATE sessions SET ejected = 1 WHERE id = ?", (session_id,))
            self.sessionModel.mark_ejected(session_id)
        else:
            self.toast.emit("Could not eject -- it may still be in use.")

    @Slot(str)
    def importNow(self, folder_url: str) -> None:
        path = QUrl(folder_url).toLocalFile() or folder_url
        if not path:
            return
        label = Path(path).name or path
        self._import_worker.submit(ImportRequest(source_root=path, device_label=label, kind="manual"))

    # --- notifications --------------------------------------------------------------

    def _on_notification_open(self, session_id: int) -> None:
        row = self._conn.execute(
            "SELECT device_label FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        self.navigateToSession.emit(session_id, row["device_label"] if row else "")

    @Slot(int)
    def markNotificationRead(self, notification_id: int) -> None:
        notifications.mark_read(self._conn, notification_id)
        self.notificationModel.load(self._conn)
        self.unreadNotificationCountChanged.emit()

    @Slot()
    def markAllNotificationsRead(self) -> None:
        notifications.mark_all_read(self._conn)
        self.notificationModel.load(self._conn)
        self.unreadNotificationCountChanged.emit()

    @Slot()
    def clearNotifications(self) -> None:
        notifications.clear_all(self._conn)
        self.notificationModel.load(self._conn)
        self.unreadNotificationCountChanged.emit()

    def _unread_notification_count(self) -> int:
        return self.notificationModel.unread_count()

    unreadCount = Property(int, _unread_notification_count, notify=unreadNotificationCountChanged)

    # --- theme / blur -----------------------------------------------------------------

    def _on_system_color_scheme_changed(self, scheme: int) -> None:
        prefers_dark = scheme == PREFER_DARK
        if prefers_dark != self._system_prefers_dark:
            self._system_prefers_dark = prefers_dark
            self.systemPrefersDarkChanged.emit()

    def _get_system_prefers_dark(self) -> bool:
        return self._system_prefers_dark

    systemPrefersDark = Property(bool, _get_system_prefers_dark, notify=systemPrefersDarkChanged)

    def _blur_supported(self) -> bool:
        return self._blur_available

    blurSupported = Property(bool, _blur_supported, constant=True)

    # --- dnglab setup ------------------------------------------------------------------

    def _start_dnglab_setup(self) -> None:
        if self._dnglab_worker is not None and self._dnglab_worker.isRunning():
            return
        self._dnglab_worker = dnglab_setup.EnsureWorker(self)
        self._dnglab_worker.finishedOk.connect(self._on_dnglab_setup_done)
        self._dnglab_worker.start()

    def _on_dnglab_setup_done(self, ok: bool, path: str) -> None:
        self._dnglab_ready = ok
        if ok:
            settings_store.set(self._conn, "dnglab_path", path)
        self.dnglabReadyChanged.emit()

    @Slot()
    def retryDnglabSetup(self) -> None:
        self._start_dnglab_setup()

    def _dnglab_ready_get(self) -> bool:
        return self._dnglab_ready

    dnglabReady = Property(bool, _dnglab_ready_get, notify=dnglabReadyChanged)

    # --- settings ----------------------------------------------------------------------

    @Slot(str, result='QVariant')
    def getSetting(self, key: str):
        return settings_store.get(self._conn, key)

    @Slot(str, 'QVariant')
    def setSetting(self, key: str, value) -> None:
        settings_store.set(self._conn, key, value)
        if key == "watch_enabled":
            self._watch_enabled = bool(value)
            if self._watch_enabled:
                mtp_enabled = bool(settings_store.get(self._conn, "mtp_enabled"))
                self._device_watcher.start(mtp_enabled=mtp_enabled)
            else:
                self._device_watcher.stop()
            self.watchEnabledChanged.emit()
        elif key == "mtp_enabled":
            self._device_watcher.set_mtp_enabled(bool(value))

    def _watch_enabled_get(self) -> bool:
        return self._watch_enabled

    watchEnabled = Property(bool, _watch_enabled_get, notify=watchEnabledChanged)

    @Slot(result=str)
    def defaultLibraryRoot(self) -> str:
        return str(paths.default_library_root())

    @Slot(str)
    def setLibraryRoot(self, folder_url: str) -> None:
        path = QUrl(folder_url).toLocalFile() or folder_url
        if path:
            settings_store.set(self._conn, "library_root", path)
