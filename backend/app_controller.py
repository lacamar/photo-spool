"""QObject facade exposed to QML as `appController`. Owns the DB connection
(GUI thread only) and orchestrates every backend module."""
from __future__ import annotations

import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from . import __version__, db, device_watch, dnglab_setup, notifications, paths, scanner, settings_store
from .blur import compositor_supports_blur
from .import_worker import ImportRequest, ImportWorker
from .list_models import NotificationListModel, SessionListModel, SourceListModel
from .notifications import NotificationManager
from .preview_worker import PreviewWorker
from .stats_worker import SourceStatsWorker
from .theme_portal import PREFER_DARK, ThemePortal

try:
    import dbus
except ImportError:  # pragma: no cover - dbus-python always present via dnf dep
    dbus = None

logger = logging.getLogger(__name__)


class AppController(QObject):
    systemPrefersDarkChanged = Signal()
    unreadNotificationCountChanged = Signal()
    dnglabReadyChanged = Signal()
    watchEnabledChanged = Signal()
    navigateToSession = Signal(int, str)  # session_id, device_label
    previewReady = Signal(str, list)  # source_key, items
    previewFailed = Signal(str, str)  # source_key, error message
    activeSessionChanged = Signal()
    importsPausedChanged = Signal()
    toast = Signal(str)

    def __init__(self, app, demo: bool = False, parent=None):
        super().__init__(parent)
        self._app = app
        self._conn = db.connect()
        self._reconcile_interrupted_sessions()
        if demo:
            from . import demo as demo_module
            demo_module.seed_if_empty(self._conn)

        self.sessionModel = SessionListModel(self)
        self.notificationModel = NotificationListModel(self)
        self.sourcesModel = SourceListModel(self)
        self.sessionModel.load(self._conn)
        self.notificationModel.load(self._conn)
        for row in self._conn.execute("SELECT * FROM saved_folders ORDER BY sort_order"):
            self.sourcesModel.upsert(f"folder:{row['id']}", row["label"], "folder", True, row["path"], True)

        self._notification_manager = NotificationManager(self)
        self._notification_manager.openRequested.connect(self._on_notification_open)
        self._theme_portal = ThemePortal(self)
        self._system_prefers_dark = self._theme_portal.read_color_scheme() == PREFER_DARK
        self._theme_portal.colorSchemeChanged.connect(self._on_system_color_scheme_changed)

        self._blur_available = compositor_supports_blur()

        # Live "what's happening right now" state for the top status bar --
        # kept as plain fields rather than making QML dig through
        # sessionModel, since only one session is ever actually running
        # (ImportWorker processes its queue one at a time).
        self._active_session_id = -1
        self._active_label = ""
        self._active_phase = ""
        self._active_done = 0
        self._active_total = 0
        self._active_file = ""
        self._queued_count = 0
        self._imports_paused = False
        # source_root values with a queued or running session right now --
        # ImportWorker itself already serializes everything onto one
        # thread (never truly concurrent), but nothing stopped submitting
        # a second request for a source that already has one in flight
        # (e.g. reopening the picker and clicking Import again before the
        # first request even starts), which just wastefully re-scans/
        # re-hashes the same files back to back. Guarded at submission
        # time instead.
        self._in_flight_roots: set[str] = set()

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

        self._preview_worker = PreviewWorker(self)
        self._preview_worker.previewReady.connect(self.previewReady)
        self._preview_worker.previewFailed.connect(self.previewFailed)
        self._preview_worker.start()

        self._stats_worker = SourceStatsWorker(self)
        self._stats_worker.statsReady.connect(self.sourcesModel.set_stats)
        self._stats_worker.start()
        # The saved folders upserted above are always "mounted" (a plain
        # local path), so their stats can be requested right away.
        for entry in self.sourcesModel.all_entries():
            self._stats_worker.request(entry["sourceKey"], entry["rootPath"])

        # The watcher itself always runs -- the source strip (attached
        # devices/cameras, mounted or not) should stay live regardless of
        # whether *automatic* importing is on. `watch_enabled` only gates
        # whether `_on_source_found` fires an auto-import (see below);
        # `mtp_enabled` genuinely stops MTP polling, since that toggle is
        # about not touching that subsystem at all, not about auto-import.
        self._device_watcher = device_watch.DeviceWatcher(self)
        self._device_watcher.sourceFound.connect(self._on_source_found)
        self._device_watcher.sourceAdded.connect(self._on_device_source_added)
        self._device_watcher.sourceRemoved.connect(self.sourcesModel.remove)
        self._device_watcher.sourceMountChanged.connect(self._on_source_mount_changed)
        settings = settings_store.all_settings(self._conn)
        self._watch_enabled = bool(settings.get("watch_enabled", True))
        self._device_watcher.start(mtp_enabled=bool(settings.get("mtp_enabled", True)))

    def _reconcile_interrupted_sessions(self) -> None:
        """Any session still marked 'running' at startup means the
        previous process died before finishing it -- a crash, an OOM
        kill, or (confirmed happening for real while debugging iPhone
        import) a service restart landing mid-scan. Nothing will ever
        resume it (ImportWorker's queue is in-memory only), so mark it
        failed instead of leaving a permanently-stuck "running" card in
        the history."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET status = 'failed', finished_at = ?, "
                "error_message = 'Import was interrupted (app restarted or crashed)' "
                "WHERE status = 'running'",
                (now,),
            )

    def shutdown(self) -> None:
        self._device_watcher.stop()
        self._import_worker.request_stop()
        self._import_worker.wait(5000)
        self._preview_worker.request_stop()
        self._preview_worker.wait(5000)
        self._stats_worker.request_stop()
        self._stats_worker.wait(5000)
        if self._dnglab_worker is not None:
            self._dnglab_worker.wait(1000)
        self._conn.close()

    # --- device detection / import sessions --------------------------------------

    def _on_device_source_added(self, key: str, label: str, kind: str, mounted: bool, root: str) -> None:
        self.sourcesModel.upsert(key, label, kind, mounted, root, False)
        if mounted:
            self._stats_worker.request(key, root)

    def _on_source_mount_changed(self, key: str, mounted: bool, root: str) -> None:
        self.sourcesModel.set_mounted(key, mounted, root)
        if mounted:
            self._stats_worker.request(key, root)

    @Slot()
    def refreshDevices(self) -> None:
        self._device_watcher.poll_now()
        # Also re-scan stats for everything already known to be mounted --
        # `poll_now()` only tells us about *changes* (new/removed/mount
        # state flips), not "this card may have new photos on it now".
        for entry in self.sourcesModel.all_entries():
            if entry["mounted"] and entry["rootPath"]:
                self._stats_worker.request(entry["sourceKey"], entry["rootPath"])

    def _on_source_found(self, root: str, label: str, kind: str) -> None:
        if not bool(settings_store.get(self._conn, "watch_enabled")):
            return
        text = f"{label}: importing new photos…"
        # Transient only (no in-app history entry, and the "transient" hint
        # tells the desktop's own notification daemon not to keep it in its
        # history/notification-center panel either) -- this fires on every
        # device plug-in and would otherwise clog both; the import_complete
        # notification that follows shortly after is the one worth keeping.
        self._notification_manager.send("Photo Import", text, transient=True)
        self._submit_import(ImportRequest(source_root=root, device_label=label, kind=kind))

    def _submit_import(self, request: ImportRequest) -> bool:
        if request.source_root in self._in_flight_roots:
            self.toast.emit(f"{request.device_label} is already importing.")
            return False
        self._in_flight_roots.add(request.source_root)
        self._queued_count += 1
        self._import_worker.submit(request)
        self.activeSessionChanged.emit()
        return True

    def _submit_mark_only(self, request: ImportRequest) -> bool:
        # Deliberately bypasses _queued_count/activeSessionChanged --
        # "mark as already imported" should never show up in the top
        # status bar or the "+N more queued" text. See
        # _on_session_started/_on_session_finished for the matching
        # skip on the way out.
        if request.source_root in self._in_flight_roots:
            self.toast.emit(f"{request.device_label} is already busy.")
            return False
        self._in_flight_roots.add(request.source_root)
        self._import_worker.submit(request)
        return True

    def _on_session_started(self, session_id: int) -> None:
        row = self._conn.execute(
            "SELECT device_label, mark_only FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or row["mark_only"]:
            return  # quiet: never a history card, never takes over the status bar
        self.sessionModel.upsert(session_id, self._conn)
        self._queued_count = max(self._queued_count - 1, 0)
        self._active_session_id = session_id
        self._active_label = row["device_label"]
        self._active_phase = ""
        self._active_done = 0
        self._active_total = 0
        self._active_file = ""
        self.activeSessionChanged.emit()

    def _on_session_progress(self, session_id: int, phase: str, done: int, total: int, filename: str) -> None:
        self.sessionModel.set_progress(session_id, phase, done, total, filename)
        if session_id == self._active_session_id:
            self._active_phase = phase
            self._active_done = done
            self._active_total = total
            self._active_file = filename
            self.activeSessionChanged.emit()

    def _on_session_finished(self, session_id: int) -> None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return
        self._in_flight_roots.discard(row["source_root"])
        # Re-scan the source's card so its "N new" count reflects what
        # just happened, without waiting for the user to hit refresh --
        # for both real imports and quiet mark-as-owned sessions.
        if row["source_root"]:
            for entry in self.sourcesModel.all_entries():
                if entry["mounted"] and entry["rootPath"] == row["source_root"]:
                    self._stats_worker.request(entry["sourceKey"], entry["rootPath"])
        if row["mark_only"]:
            return  # quiet: no history card, no notification, status bar was never touched
        self.sessionModel.upsert(session_id, self._conn)
        if session_id == self._active_session_id:
            self._active_session_id = -1
            self._active_label = ""
            self._active_phase = ""
            self._active_done = 0
            self._active_total = 0
            self._active_file = ""
            self.activeSessionChanged.emit()
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

    @Slot(result='QVariantMap')
    def getLibraryStats(self) -> dict:
        """Lifetime stats from the `imports` table -- the persistent dedup
        ledger, which (unlike the sessions/session_files history) is never
        touched by clearing history, so these numbers survive that. Rows
        with an empty dest_path are "mark as already imported" entries
        (nothing was ever actually copied by this app) and are counted
        separately rather than folded into the totals below."""
        totals = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(dest_bytes), 0) AS dest_bytes, "
            "COALESCE(SUM(source_bytes), 0) AS source_bytes, MIN(captured_at) AS earliest, "
            "MAX(captured_at) AS latest FROM imports WHERE dest_path != ''"
        ).fetchone()
        marked_only_count = self._conn.execute(
            "SELECT COUNT(*) AS n FROM imports WHERE dest_path = ''"
        ).fetchone()["n"]
        by_model_rows = self._conn.execute(
            "SELECT camera_model, COUNT(*) AS n, COALESCE(SUM(dest_bytes), 0) AS bytes FROM imports "
            "WHERE dest_path != '' GROUP BY camera_model ORDER BY n DESC"
        ).fetchall()
        dest_paths = self._conn.execute(
            "SELECT dest_path FROM imports WHERE dest_path != ''"
        ).fetchall()
        video_count = sum(1 for r in dest_paths if scanner.is_video(Path(r["dest_path"])))
        return {
            "totalCount": totals["n"],
            "totalBytes": totals["dest_bytes"],
            "bytesSaved": max(totals["source_bytes"] - totals["dest_bytes"], 0),
            "earliestCapturedAt": totals["earliest"] or "",
            "latestCapturedAt": totals["latest"] or "",
            "photoCount": totals["n"] - video_count,
            "videoCount": video_count,
            "markedOnlyCount": marked_only_count,
            "byModel": [
                {"model": r["camera_model"] or "Unknown", "count": r["n"], "bytes": r["bytes"]}
                for r in by_model_rows
            ],
        }

    @Slot(str)
    def openFile(self, dest_path: str) -> None:
        """Opens an imported file itself in the desktop's default handler
        (an image viewer for a .dng, in the normal case)."""
        if not dest_path:
            return
        try:
            subprocess.Popen(["xdg-open", dest_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.toast.emit("Could not open the file.")

    @Slot(str)
    def revealInFileBrowser(self, dest_path: str) -> None:
        """Opens the file's containing folder in the desktop's file
        manager with the file itself selected, via the freedesktop
        org.freedesktop.FileManager1 ShowItems method -- the standard
        cross-file-manager way to do this (supported by Nautilus, Nemo,
        Dolphin, ...). xdg-open has no equivalent: it can only open a
        folder, never select an item inside it. Falls back to just
        opening the parent folder if no file manager on the session bus
        implements that interface."""
        if not dest_path:
            return
        if dbus is not None:
            try:
                bus = dbus.SessionBus()
                proxy = bus.get_object("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1")
                iface = dbus.Interface(proxy, "org.freedesktop.FileManager1")
                iface.ShowItems([QUrl.fromLocalFile(dest_path).toString()], "")
                return
            except dbus.exceptions.DBusException:
                pass
        folder = str(Path(dest_path).parent)
        try:
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.toast.emit("Could not open the file browser.")

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

    @Slot(int)
    def clearSession(self, session_id: int) -> None:
        """Removes one entry from the import history. This only forgets
        the app's record of the session (and its per-file rows, via
        ON DELETE CASCADE) -- it never touches the imported photos
        themselves on disk."""
        if session_id == self._active_session_id:
            return  # never clear a session that's still running
        with self._conn:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.sessionModel.remove(session_id)

    @Slot()
    def clearHistory(self) -> None:
        """Clears every finished session from the history list, leaving
        a currently-running one (if any) in place."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM sessions WHERE id != ?", (self._active_session_id,)
            )
        self.sessionModel.remove_all_except(self._active_session_id)

    # --- source strip: live devices + saved folders ---------------------------------

    @Slot(str)
    def addFolder(self, folder_url: str) -> None:
        path = QUrl(folder_url).toLocalFile() or folder_url
        if not path:
            return
        label = Path(path).name or path
        now = datetime.now(timezone.utc).isoformat()
        next_order = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM saved_folders"
        ).fetchone()["n"]
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO saved_folders (path, label, sort_order, created_at) VALUES (?, ?, ?, ?)",
                    (path, label, next_order, now),
                )
        except sqlite3.IntegrityError:
            self.toast.emit("That folder is already added.")
            return
        key = f"folder:{cur.lastrowid}"
        self.sourcesModel.upsert(key, label, "folder", True, path, True)
        self._stats_worker.request(key, path)

    @Slot(str)
    def removeFolder(self, source_key: str) -> None:
        if not source_key.startswith("folder:"):
            return
        folder_id = int(source_key.removeprefix("folder:"))
        with self._conn:
            self._conn.execute("DELETE FROM saved_folders WHERE id = ?", (folder_id,))
        self.sourcesModel.remove(source_key)

    @Slot(str)
    def requestPreview(self, source_key: str) -> None:
        """Scans a source for importable files and their dedup status (fast --
        metadata + a cheap pre-check, no content hashing) and reports the
        result via `previewReady`. Mounts the device first if needed --
        this is also how "mount when prompted" happens: the prompt is the
        click."""
        entry = self.sourcesModel.entry_for(source_key)
        if entry is None:
            self.previewFailed.emit(source_key, "That source is no longer available.")
            return
        root = entry["rootPath"]
        if not root and entry["kind"] == "blockdev":
            root = self._device_watcher.mount_source(source_key)
        if not root:
            self.previewFailed.emit(source_key, "Could not mount this device.")
            return
        self._preview_worker.request(source_key, root)

    def _resolve_selection(self, source_key: str, filenames: list) -> tuple[dict, frozenset] | None:
        entry = self.sourcesModel.entry_for(source_key)
        if entry is None or not entry["rootPath"]:
            self.toast.emit("That source is no longer available.")
            return None
        names = frozenset(str(f) for f in filenames)
        if not names:
            return None
        return entry, names

    @Slot(str, 'QVariantList')
    def importSelected(self, source_key: str, filenames: list) -> None:
        resolved = self._resolve_selection(source_key, filenames)
        if resolved is None:
            return
        entry, names = resolved
        # "iphone" is a display-only kind (nicer icon in the source strip)
        # -- the sessions table's CHECK constraint only allows
        # blockdev/mtp/manual, so it collapses back to "mtp" here.
        kind = {"folder": "manual", "iphone": "mtp"}.get(entry["kind"], entry["kind"])
        self._submit_import(ImportRequest(
            source_root=entry["rootPath"], device_label=entry["label"], kind=kind, selected_filenames=names,
        ))

    @Slot(str, 'QVariantList')
    def markAlreadyImported(self, source_key: str, filenames: list) -> None:
        """Records these files in the dedup ledger without converting or
        copying anything -- for photos this app never imported itself
        (e.g. ones Lightroom already handled) that would otherwise show up
        as "new" every time this source is scanned. Deliberately quiet: no
        history card, no notification -- see _submit_mark_only."""
        resolved = self._resolve_selection(source_key, filenames)
        if resolved is None:
            return
        entry, names = resolved
        kind = {"folder": "manual", "iphone": "mtp"}.get(entry["kind"], entry["kind"])
        self._submit_mark_only(ImportRequest(
            source_root=entry["rootPath"], device_label=entry["label"] + " (marked, not imported)",
            kind=kind, selected_filenames=names, mark_only=True,
        ))

    @Slot(str, 'QVariantList')
    def unmarkImported(self, source_key: str, filenames: list) -> None:
        """Reverses markAlreadyImported (or forgets a genuine past import
        -- this only ever forgets the dedup-ledger record, never touches
        an already-placed file): these files show up as new again on the
        next scan. No hashing needed -- a cheap metadata-only re-scan is
        enough to recover the same (camera_model, filename, size) key
        quick_duplicate_match/markAlreadyImported used to record it, so
        this is effectively instant, unlike a real import."""
        resolved = self._resolve_selection(source_key, filenames)
        if resolved is None:
            return
        entry, names = resolved
        root = Path(entry["rootPath"])
        try:
            files = [f for f in scanner.find_importable_files(root) if f.name in names]
        except OSError:
            self.toast.emit("That source is no longer available.")
            return
        metadata = scanner.read_metadata(files)
        removed = 0
        with self._conn:
            for f in files:
                cand = metadata.get(f)
                if cand is None:
                    continue
                cur = self._conn.execute(
                    "DELETE FROM imports WHERE camera_model = ? AND source_filename = ? AND source_bytes = ?",
                    (cand.camera_model, f.name, cand.size_bytes),
                )
                removed += cur.rowcount
        if removed:
            self.toast.emit(f"Unmarked {removed} file{'s' if removed != 1 else ''} -- they'll show as new again.")
        self._stats_worker.request(source_key, entry["rootPath"])

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

    # --- active-session status, for the persistent top status bar -------------------

    activeSessionId = Property(int, lambda self: self._active_session_id, notify=activeSessionChanged)
    activeLabel = Property(str, lambda self: self._active_label, notify=activeSessionChanged)
    activePhase = Property(str, lambda self: self._active_phase, notify=activeSessionChanged)
    activeDone = Property(int, lambda self: self._active_done, notify=activeSessionChanged)
    activeTotal = Property(int, lambda self: self._active_total, notify=activeSessionChanged)
    activeFile = Property(str, lambda self: self._active_file, notify=activeSessionChanged)
    queuedCount = Property(int, lambda self: self._queued_count, notify=activeSessionChanged)

    @Slot(bool)
    def setImportsPaused(self, paused: bool) -> None:
        self._imports_paused = paused
        self._import_worker.set_paused(paused)
        self.importsPausedChanged.emit()

    importsPaused = Property(bool, lambda self: self._imports_paused, notify=importsPausedChanged)

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

    appVersion = Property(str, lambda self: __version__, constant=True)

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
