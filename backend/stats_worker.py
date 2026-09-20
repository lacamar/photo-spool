"""Background per-source stats for the source strip's cards: photo counts
(total / not-yet-imported) and disk capacity. Runs on its own QThread with
its own sqlite3 connection, same pattern as PreviewWorker, but as a FIFO
queue rather than "latest wins" -- unlike the preview panel (only ever one
open at a time), several source cards can legitimately want their stats
refreshed together (e.g. after a manual refresh), and none of them should
cancel another."""
from __future__ import annotations

import logging
import shutil
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QMutex, QThread, QWaitCondition, Signal

from . import db, scanner

logger = logging.getLogger(__name__)


class SourceStatsWorker(QThread):
    statsReady = Signal(str, dict)  # source_key, stats

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._condition = QWaitCondition()
        self._queue: dict[str, str] = {}  # source_key -> root, insertion order preserved
        self._stop_requested = False

    def request(self, source_key: str, root: str) -> None:
        if not root:
            return
        self._mutex.lock()
        self._queue[source_key] = root
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
                while not self._queue and not self._stop_requested:
                    self._condition.wait(self._mutex)
                if self._stop_requested:
                    self._mutex.unlock()
                    break
                source_key = next(iter(self._queue))
                root = self._queue.pop(source_key)
                self._mutex.unlock()

                try:
                    stats = self._compute(conn, root)
                    self.statsReady.emit(source_key, stats)
                except Exception:
                    logger.exception("Stats scan failed for %s", root)
        finally:
            conn.close()

    def _compute(self, conn, root_str: str) -> dict:
        capacity_bytes = used_bytes = free_bytes = 0
        try:
            usage = shutil.disk_usage(root_str)
            capacity_bytes, used_bytes, free_bytes = usage.total, usage.used, usage.free
        except OSError:
            pass  # gvfs (MTP/AFC) mounts don't always support statvfs -- stats-less card is fine

        file_count = new_count = 0
        content_bytes = 0
        models: Counter[str] = Counter()
        root = Path(root_str)
        if root.is_dir():
            files = scanner.find_importable_files(root)
            metadata = scanner.read_metadata(files, conn)
            file_count = len(files)
            for f in files:
                cand = metadata.get(f)
                if cand is None:
                    continue
                content_bytes += cand.size_bytes
                if cand.camera_model:
                    models[cand.camera_model] += 1
                if not scanner.quick_duplicate_check(conn, cand.camera_model, f.name, cand.size_bytes):
                    new_count += 1

        return {
            "fileCount": file_count,
            "newCount": new_count,
            "contentBytes": content_bytes,
            "cameraModel": models.most_common(1)[0][0] if models else "",
            "capacityBytes": capacity_bytes,
            "usedBytes": used_bytes,
            "freeBytes": free_bytes,
        }
