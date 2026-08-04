"""Runs the import pipeline (scan -> dedup -> convert -> place -> record)
for one device/mount at a time, on its own QThread with its own sqlite3
connection (connections aren't safe to share across threads). Requests are
queued rather than run concurrently, so a device that appears mid-import
just waits its turn instead of racing dnglab or the DB.
"""
from __future__ import annotations

import logging
import queue
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from . import converter, db, dnglab_setup, paths, scanner, settings_store

logger = logging.getLogger(__name__)

_STOP = object()


@dataclass
class ImportRequest:
    source_root: str
    device_label: str
    kind: str  # 'blockdev' | 'mtp' | 'manual'


class ImportWorker(QThread):
    sessionStarted = Signal(int)  # session_id
    sessionProgress = Signal(int, int, int, str)  # session_id, done, total, current_filename
    sessionFinished = Signal(int)  # session_id
    dnglabUnavailable = Signal(int)  # session_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: "queue.Queue" = queue.Queue()

    def submit(self, request: ImportRequest) -> None:
        self._queue.put(request)

    def request_stop(self) -> None:
        self._queue.put(_STOP)

    def run(self) -> None:
        conn = db.connect()
        try:
            while True:
                request = self._queue.get()
                if request is _STOP:
                    break
                try:
                    self._run_session(conn, request)
                except Exception:
                    logger.exception("Import session failed")
        finally:
            conn.close()

    def _run_session(self, conn, request: ImportRequest) -> None:
        started_at = datetime.now(timezone.utc).isoformat()
        with conn:
            cur = conn.execute(
                "INSERT INTO sessions (started_at, device_label, source_root, kind, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (started_at, request.device_label, request.source_root, request.kind),
            )
        session_id = cur.lastrowid
        self.sessionStarted.emit(session_id)

        settings = settings_store.all_settings(conn)
        library_root = Path(settings["library_root"])

        try:
            files = scanner.find_arw_files(Path(request.source_root))
        except OSError as exc:
            self._finish_session(conn, session_id, "failed", error_message=str(exc))
            return

        with conn:
            conn.execute("UPDATE sessions SET found_count = ? WHERE id = ?", (len(files), session_id))
        if not files:
            self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
            return

        dnglab_path = dnglab_setup.ensure()
        if dnglab_path is None:
            self._finish_session(
                conn, session_id, "failed",
                error_message="The DNG converter (dnglab) isn't available -- check your network "
                               "connection, then retry from Settings.",
            )
            self.dnglabUnavailable.emit(session_id)
            return

        metadata = scanner.read_metadata(files)
        sort_order = 0
        to_stage: list[tuple[str, scanner.Candidate, int]] = []  # (source_hash, candidate, sort_order)

        for f in files:
            cand = metadata.get(f)
            if cand is None:
                self._record_file(conn, session_id, f.name, "failed", "", "Could not read file metadata",
                                   sort_order)
                sort_order += 1
                continue
            if scanner.quick_duplicate_check(conn, cand.camera_model, f.name, cand.size_bytes):
                self._record_file(conn, session_id, f.name, "duplicate", "", "", sort_order)
                sort_order += 1
                continue
            try:
                source_hash = scanner.hash_file(cand.path)
            except OSError as exc:
                self._record_file(conn, session_id, f.name, "failed", "", str(exc), sort_order)
                sort_order += 1
                continue
            existing_dest = scanner.hash_duplicate_check(conn, source_hash)
            if existing_dest is not None:
                self._record_file(conn, session_id, f.name, "duplicate", existing_dest, "", sort_order)
                sort_order += 1
                continue
            to_stage.append((source_hash, cand, sort_order))
            sort_order += 1

        if not to_stage:
            self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
            return

        staging_root = paths.staging_dir() / f"session-{session_id}"
        staging_in = staging_root / "in"
        staging_out = staging_root / "out"
        staging_in.mkdir(parents=True, exist_ok=True)

        staged: list[tuple[str, scanner.Candidate, int]] = []
        for source_hash, cand, order in to_stage:
            link = staging_in / f"{source_hash}.ARW"
            try:
                link.symlink_to(cand.path)
            except OSError as exc:
                self._record_file(conn, session_id, cand.path.name, "failed", "", str(exc), order)
                continue
            staged.append((source_hash, cand, order))

        compression = settings.get("dng_compression", "lossless")
        embed_raw = bool(settings.get("embed_raw_in_dng", False))
        self.sessionProgress.emit(session_id, 0, len(staged), "Converting to DNG…")
        try:
            result = converter.convert_batch(dnglab_path, staging_in, staging_out, compression, embed_raw)
        except (OSError, subprocess.TimeoutExpired) as exc:
            for source_hash, cand, order in staged:
                self._record_file(conn, session_id, cand.path.name, "failed", "", str(exc), order)
            shutil.rmtree(staging_root, ignore_errors=True)
            self._finish_session(conn, session_id, "failed", error_message=str(exc))
            return

        bytes_saved = 0
        delete_originals = bool(settings.get("delete_originals_after_import"))
        for i, (source_hash, cand, order) in enumerate(staged):
            self.sessionProgress.emit(session_id, i + 1, len(staged), cand.path.name)
            converted = staging_out / f"{source_hash}.dng"
            if not converted.is_file():
                snippet = (result.stderr or result.stdout or "conversion failed").strip()[-300:]
                self._record_file(conn, session_id, cand.path.name, "failed", "", snippet, order)
                continue

            dest = converter.unique_dest_path(converter.library_dest_path(library_root, cand))
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(converted), str(dest))
            except OSError as exc:
                self._record_file(conn, session_id, cand.path.name, "failed", "", str(exc), order)
                continue

            dest_bytes = dest.stat().st_size
            bytes_saved += max(cand.size_bytes - dest_bytes, 0)
            now = datetime.now(timezone.utc).isoformat()
            with conn:
                conn.execute(
                    "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, "
                    "captured_at, dest_path, dest_bytes, session_id, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_hash, cand.path.name, cand.size_bytes, cand.camera_model,
                     cand.captured_at, str(dest), dest_bytes, session_id, now),
                )
            self._record_file(conn, session_id, cand.path.name, "imported", str(dest), "", order)

            if delete_originals:
                try:
                    cand.path.unlink()
                except OSError:
                    logger.info("Could not delete original %s after import", cand.path, exc_info=True)

        shutil.rmtree(staging_root, ignore_errors=True)
        with conn:
            conn.execute("UPDATE sessions SET bytes_saved = bytes_saved + ? WHERE id = ?",
                         (bytes_saved, session_id))
        self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)

    def _record_file(self, conn, session_id: int, filename: str, status: str, dest_path: str,
                      error_message: str, sort_order: int) -> None:
        with conn:
            conn.execute(
                "INSERT INTO session_files (session_id, source_filename, status, dest_path, "
                "error_message, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, filename, status, dest_path, error_message, sort_order),
            )
            column = {"imported": "imported_count", "duplicate": "duplicate_count",
                      "failed": "failed_count"}[status]
            conn.execute(f"UPDATE sessions SET {column} = {column} + 1 WHERE id = ?", (session_id,))

    def _finish_session(self, conn, session_id: int, status: str, error_message: str = "",
                         ejectable_path: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                "UPDATE sessions SET status = ?, finished_at = ?, error_message = ?, ejectable_path = ? "
                "WHERE id = ?",
                (status, now, error_message, ejectable_path, session_id),
            )
        self.sessionFinished.emit(session_id)
