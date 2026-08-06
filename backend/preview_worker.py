"""Fast, read-only preview scan for the source picker: metadata plus a
cheap (model, filename, size) dedup pre-check only -- no content hashing,
which real-world timing showed is the genuinely slow part of an import
(roughly 1s/file, bound by the card reader, not CPU -- see
import_worker.py). That's good enough to grey out obviously-already-
imported files in the picker; the real import still runs the
authoritative hash-based dedup before writing anything.

Runs on its own QThread with its own sqlite3 connection. Only the latest
requested source is ever scanned -- clicking through several devices
quickly just supersedes the pending request rather than queueing a scan
for a panel the user has already navigated away from.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QMutex, QThread, QWaitCondition, Signal

from . import db, scanner

logger = logging.getLogger(__name__)


class PreviewWorker(QThread):
    previewReady = Signal(str, list)  # source_key, items
    previewFailed = Signal(str, str)  # source_key, error message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._condition = QWaitCondition()
        self._pending: tuple[str, str] | None = None  # (source_key, root)
        self._stop_requested = False

    def request(self, source_key: str, root: str) -> None:
        self._mutex.lock()
        self._pending = (source_key, root)
        self._condition.wakeOne()
        self._mutex.unlock()

    def request_stop(self) -> None:
        self._mutex.lock()
        self._stop_requested = True
        self._condition.wakeOne()
        self._mutex.unlock()

    def run(self) -> None:
        conn = db.connect()
        try:
            while True:
                self._mutex.lock()
                while self._pending is None and not self._stop_requested:
                    self._condition.wait(self._mutex)
                if self._stop_requested:
                    self._mutex.unlock()
                    break
                source_key, root = self._pending
                self._pending = None
                self._mutex.unlock()

                try:
                    items = self._scan(conn, root)
                    self.previewReady.emit(source_key, items)
                except Exception as exc:
                    logger.exception("Preview scan failed for %s", root)
                    self.previewFailed.emit(source_key, str(exc))
        finally:
            conn.close()

    def _scan(self, conn, root: str) -> list[dict]:
        files = scanner.find_importable_files(Path(root))
        metadata = scanner.read_metadata(files)
        items = []
        for f in files:
            cand = metadata.get(f)
            if cand is None:
                continue
            already = scanner.quick_duplicate_check(conn, cand.camera_model, f.name, cand.size_bytes)
            items.append({
                "filename": f.name,
                "path": str(f),
                "sizeBytes": cand.size_bytes,
                "capturedAt": cand.captured_at or "",
                "alreadyImported": already,
            })
        items.sort(key=lambda it: it["filename"])
        return items
