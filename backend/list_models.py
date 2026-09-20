"""List models exposed to QML: import-session history and the source
strip. Per-session file lists (shown in the session detail popup) are
small and viewed on demand rather than kept live, so they're just a plain
Slot returning QVariantList on AppController -- no model class needed."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Qt

from .models import SESSION_KIND_LABELS

SESSION_ROLES = [
    "sessionId", "startedAt", "finishedAt", "deviceLabel", "sourceRoot", "kind", "kindLabel",
    "status", "foundCount", "importedCount", "duplicateCount", "failedCount", "bytesSaved",
    "errorMessage", "startedDay",
    "progressPhase", "progressDone", "progressTotal", "progressFile",
]
_SESSION_BASE = Qt.UserRole + 1
SESSION_ROLE_MAP = {n: _SESSION_BASE + i for i, n in enumerate(SESSION_ROLES)}


def _local_day(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().date().isoformat()
    except ValueError:
        return ""


def _session_row_to_entry(row: sqlite3.Row) -> dict:
    return {
        "sessionId": row["id"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"] or "",
        "deviceLabel": row["device_label"],
        "sourceRoot": row["source_root"],
        "kind": row["kind"],
        "kindLabel": SESSION_KIND_LABELS.get(row["kind"], row["kind"]),
        "status": row["status"],
        "foundCount": row["found_count"],
        "importedCount": row["imported_count"],
        "duplicateCount": row["duplicate_count"],
        "failedCount": row["failed_count"],
        "bytesSaved": row["bytes_saved"],
        "errorMessage": row["error_message"],
        "startedDay": _local_day(row["started_at"]),
        "progressPhase": "",
        "progressDone": 0,
        "progressTotal": 0,
        "progressFile": "",
    }


class SessionListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[dict] = []

    def load(self, conn: sqlite3.Connection) -> None:
        # mark_only ("Have it") sessions are deliberately quiet -- see
        # AppController._on_session_started/_finished, which never call
        # upsert() for one during a live run. Filtered here too so an
        # app restart doesn't suddenly surface old ones as history cards.
        self.beginResetModel()
        self._entries = [
            _session_row_to_entry(row)
            for row in conn.execute("SELECT * FROM sessions WHERE mark_only = 0 ORDER BY id DESC")
        ]
        self.endResetModel()

    def index_of(self, session_id: int) -> int:
        for i, e in enumerate(self._entries):
            if e["sessionId"] == session_id:
                return i
        return -1

    def upsert(self, session_id: int, conn: sqlite3.Connection) -> None:
        """Refresh a session's stored fields from the DB, preserving
        whatever in-memory progress fields are currently set. Brand-new
        sessions are inserted at the top (they're always the newest --
        started_at only ever increases)."""
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return
        i = self.index_of(session_id)
        entry = _session_row_to_entry(row)
        if i >= 0:
            entry["progressPhase"] = self._entries[i]["progressPhase"]
            entry["progressDone"] = self._entries[i]["progressDone"]
            entry["progressTotal"] = self._entries[i]["progressTotal"]
            entry["progressFile"] = self._entries[i]["progressFile"]
            self._entries[i] = entry
            idx = self.index(i, 0)
            self.dataChanged.emit(idx, idx)
        else:
            self.beginInsertRows(QModelIndex(), 0, 0)
            self._entries.insert(0, entry)
            self.endInsertRows()

    def set_progress(self, session_id: int, phase: str, done: int, total: int, filename: str) -> None:
        i = self.index_of(session_id)
        if i < 0:
            return
        self._entries[i]["progressPhase"] = phase
        self._entries[i]["progressDone"] = done
        self._entries[i]["progressTotal"] = total
        self._entries[i]["progressFile"] = filename
        idx = self.index(i, 0)
        self.dataChanged.emit(idx, idx)

    def remove(self, session_id: int) -> None:
        i = self.index_of(session_id)
        if i < 0:
            return
        self.beginRemoveRows(QModelIndex(), i, i)
        del self._entries[i]
        self.endRemoveRows()

    def remove_all_except(self, keep_session_id: int) -> None:
        """Clears every entry except (optionally) one still-running session."""
        for i in reversed(range(len(self._entries))):
            if self._entries[i]["sessionId"] == keep_session_id:
                continue
            self.beginRemoveRows(QModelIndex(), i, i)
            del self._entries[i]
            self.endRemoveRows()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self._entries[index.row()]
        for name, r in SESSION_ROLE_MAP.items():
            if r == role:
                return entry.get(name)
        return None

    def roleNames(self) -> dict:
        return {r: QByteArray(n.encode()) for n, r in SESSION_ROLE_MAP.items()}


# Named "rootPath", not "root": ListView delegates auto-expose every model
# role as a bare identifier in scope, and "root" is by far the most common
# `id:` name for a component's own outer Item -- a role literally called
# "root" silently shadows it, so `root.someSignal(...)` inside a delegate
# resolves to this role's string value instead of the component, and fails
# with a QML TypeError that's easy to miss (stderr only, no visual sign).
# Hit this for real in SourceStrip.qml; keep the role name collision-safe.
SOURCE_ROLES = [
    "sourceKey", "label", "kind", "mounted", "rootPath", "removable",
    "statsLoaded", "fileCount", "newCount", "contentBytes", "cameraModel",
    "capacityBytes", "usedBytes", "freeBytes",
]
_SOURCE_BASE = Qt.UserRole + 1
SOURCE_ROLE_MAP = {n: _SOURCE_BASE + i for i, n in enumerate(SOURCE_ROLES)}

_SOURCE_STATS_DEFAULTS = {
    "statsLoaded": False, "fileCount": 0, "newCount": 0, "contentBytes": 0, "cameraModel": "",
    "capacityBytes": 0, "usedBytes": 0, "freeBytes": 0,
}


class SourceListModel(QAbstractListModel):
    """Devices currently attached (live, from DeviceWatcher) plus
    manually-saved folders (persisted in the `saved_folders` table) --
    whatever the source strip shows as a clickable icon. Not DB-backed for
    the live-device rows, since attachment state only exists at runtime;
    `upsert`/`remove` are called directly from AppController's device
    signal handlers instead of a `load()`."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[dict] = []

    def index_of(self, key: str) -> int:
        for i, e in enumerate(self._entries):
            if e["sourceKey"] == key:
                return i
        return -1

    def entry_for(self, key: str) -> dict | None:
        i = self.index_of(key)
        return dict(self._entries[i]) if i >= 0 else None

    def all_entries(self) -> list[dict]:
        return [dict(e) for e in self._entries]

    def upsert(self, key: str, label: str, kind: str, mounted: bool, root: str, removable: bool) -> None:
        entry = {"sourceKey": key, "label": label, "kind": kind, "mounted": mounted, "rootPath": root,
                 "removable": removable, **_SOURCE_STATS_DEFAULTS}
        i = self.index_of(key)
        if i >= 0:
            # Preserve previously-loaded stats across a re-upsert (e.g. the
            # same device polled again) so cards don't flicker back to
            # "…" every poll cycle -- only a real mount-state change
            # should invalidate them (see set_mounted below).
            for stat_key in _SOURCE_STATS_DEFAULTS:
                entry[stat_key] = self._entries[i].get(stat_key, _SOURCE_STATS_DEFAULTS[stat_key])
            self._entries[i] = entry
            idx = self.index(i, 0)
            self.dataChanged.emit(idx, idx)
        else:
            self.beginInsertRows(QModelIndex(), len(self._entries), len(self._entries))
            self._entries.append(entry)
            self.endInsertRows()

    def set_mounted(self, key: str, mounted: bool, root: str) -> None:
        i = self.index_of(key)
        if i < 0:
            return
        self._entries[i]["mounted"] = mounted
        self._entries[i]["rootPath"] = root
        # Stats are tied to a specific mount -- stale once that changes.
        self._entries[i].update(_SOURCE_STATS_DEFAULTS)
        idx = self.index(i, 0)
        self.dataChanged.emit(idx, idx)

    def set_stats(self, key: str, stats: dict) -> None:
        i = self.index_of(key)
        if i < 0:
            return
        self._entries[i].update(stats)
        self._entries[i]["statsLoaded"] = True
        idx = self.index(i, 0)
        self.dataChanged.emit(idx, idx)

    def remove(self, key: str) -> None:
        i = self.index_of(key)
        if i < 0:
            return
        self.beginRemoveRows(QModelIndex(), i, i)
        del self._entries[i]
        self.endRemoveRows()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self._entries[index.row()]
        for name, r in SOURCE_ROLE_MAP.items():
            if r == role:
                return entry.get(name)
        return None

    def roleNames(self) -> dict:
        return {r: QByteArray(n.encode()) for n, r in SOURCE_ROLE_MAP.items()}
