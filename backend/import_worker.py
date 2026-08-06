"""Runs the import pipeline (scan -> dedup -> convert -> place -> record)
for one device/mount at a time, on its own QThread with its own sqlite3
connection (connections aren't safe to share across threads). Requests are
queued rather than run concurrently, so a device that appears mid-import
just waits its turn instead of racing dnglab or the DB.

Progress is reported in phases (see `sessionProgress`) rather than one
flat counter: real-world timing against an actual 330-file, 82MB/file card
showed dnglab conversion itself is fast (well under a second per file),
but reading+hashing the source files to dedup them is the genuinely slow
part (~1s/file, bound by the card reader's throughput, not CPU -- nothing
to parallelize there). Without a phase-aware progress signal, that whole
multi-minute span reports nothing and looks hung.
"""
from __future__ import annotations

import logging
import queue
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QMutex, QThread, QWaitCondition, Signal

from . import converter, db, dnglab_setup, paths, scanner, settings_store

logger = logging.getLogger(__name__)

_STOP = object()

PHASE_SCANNING = "scanning"
PHASE_CHECKING = "checking"
PHASE_CONVERTING = "converting"
PHASE_PLACING = "placing"


@dataclass
class ImportRequest:
    source_root: str
    device_label: str
    kind: str  # 'blockdev' | 'mtp' | 'manual'
    # None = import everything found; otherwise only these original
    # filenames are processed (the rest are left untouched, not even
    # recorded as duplicates -- the user just didn't ask for them).
    selected_filenames: frozenset[str] | None = None
    # True for "mark as already imported": still scans, reads metadata and
    # hashes each selected file (so future scans dedup them correctly),
    # but never touches dnglab or the library -- just records the dedup
    # ledger entry. Used to backfill dedup awareness for photos this app
    # never actually imported (e.g. ones Lightroom already handled).
    mark_only: bool = False


class ImportWorker(QThread):
    sessionStarted = Signal(int)  # session_id
    sessionProgress = Signal(int, str, int, int, str)  # session_id, phase, done, total, current_filename
    sessionFinished = Signal(int)  # session_id
    dnglabUnavailable = Signal(int)  # session_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: "queue.Queue" = queue.Queue()
        # Pausing can't interrupt a single dnglab batch invocation or a
        # single file's hash read mid-flight (nothing safely cancellable
        # about either), so it takes effect at the next natural boundary
        # instead: between files during the checking/placing loops, before
        # a conversion batch starts, or before the next queued session
        # starts at all. Good enough granularity in practice -- the slow
        # part (hashing) is exactly where it's checked most often.
        self._pause_mutex = QMutex()
        self._pause_condition = QWaitCondition()
        self._paused = False
        self._stopping = False

    def submit(self, request: ImportRequest) -> None:
        self._queue.put(request)

    def set_paused(self, paused: bool) -> None:
        self._pause_mutex.lock()
        self._paused = paused
        if not paused:
            self._pause_condition.wakeAll()
        self._pause_mutex.unlock()

    def _wait_if_paused(self) -> None:
        self._pause_mutex.lock()
        while self._paused and not self._stopping:
            self._pause_condition.wait(self._pause_mutex)
        self._pause_mutex.unlock()

    def request_stop(self) -> None:
        # Also wakes anything currently blocked in _wait_if_paused, so
        # shutdown doesn't hang waiting on a paused import to resume.
        self._pause_mutex.lock()
        self._stopping = True
        self._paused = False
        self._pause_condition.wakeAll()
        self._pause_mutex.unlock()
        self._queue.put(_STOP)

    def run(self) -> None:
        conn = db.connect()
        try:
            while True:
                request = self._queue.get()
                if request is _STOP:
                    break
                self._wait_if_paused()
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
            files = scanner.find_raw_files(Path(request.source_root))
        except OSError as exc:
            self._finish_session(conn, session_id, "failed", error_message=str(exc))
            return

        with conn:
            conn.execute("UPDATE sessions SET found_count = ? WHERE id = ?", (len(files), session_id))
        self.sessionProgress.emit(session_id, PHASE_SCANNING, len(files), len(files), "")
        if not files:
            self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
            return

        if request.selected_filenames is not None:
            files = [f for f in files if f.name in request.selected_filenames]
            if not files:
                self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
                return

        dnglab_path = None
        if not request.mark_only:
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

        for i, f in enumerate(files):
            self._wait_if_paused()
            self.sessionProgress.emit(session_id, PHASE_CHECKING, i + 1, len(files), f.name)
            cand = metadata.get(f)
            if cand is None:
                self._record_file(conn, session_id, f.name, "failed", "", "Could not read file metadata",
                                   sort_order)
                sort_order += 1
                continue
            quick_match = scanner.quick_duplicate_match(conn, cand.camera_model, f.name, cand.size_bytes)
            if quick_match is not None:
                self._record_file(conn, session_id, f.name, "duplicate", quick_match, "", sort_order)
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

        if request.mark_only:
            now = datetime.now(timezone.utc).isoformat()
            for source_hash, cand, order in to_stage:
                self._wait_if_paused()
                self.sessionProgress.emit(session_id, PHASE_PLACING, order + 1, len(to_stage), cand.path.name)
                with conn:
                    conn.execute(
                        "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, "
                        "captured_at, dest_path, dest_bytes, session_id, imported_at) "
                        "VALUES (?, ?, ?, ?, ?, '', 0, ?, ?)",
                        (source_hash, cand.path.name, cand.size_bytes, cand.camera_model,
                         cand.captured_at, session_id, now),
                    )
                self._record_file(conn, session_id, cand.path.name, "imported", "", "", order)
            self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
            return

        staging_root = paths.staging_dir() / f"session-{session_id}"
        staging_in = staging_root / "in"
        staging_out = staging_root / "out"
        staging_in.mkdir(parents=True, exist_ok=True)

        # DNG sources (iPhone ProRAW, or a camera that already shoots DNG)
        # need no conversion -- they're copied straight to the library.
        # Everything else is staged for dnglab, keeping the source's own
        # extension (dnglab may rely on it, not just file content, to pick
        # a decoder for less-common formats).
        to_convert: list[tuple[str, scanner.Candidate, int]] = []
        to_copy: list[tuple[str, scanner.Candidate, int]] = []
        by_hash: dict[str, tuple[scanner.Candidate, int]] = {}
        for source_hash, cand, order in to_stage:
            if scanner.is_dng(cand.path):
                to_copy.append((source_hash, cand, order))
                continue
            link = staging_in / f"{source_hash}{cand.path.suffix}"
            try:
                link.symlink_to(cand.path)
            except OSError as exc:
                self._record_file(conn, session_id, cand.path.name, "failed", "", str(exc), order)
                continue
            to_convert.append((source_hash, cand, order))
            by_hash[source_hash] = (cand, order)

        compression = settings.get("dng_compression", "lossless")
        embed_raw = bool(settings.get("embed_raw_in_dng", False))
        convert_done = 0
        total_to_place = len(to_convert) + len(to_copy)

        def on_converted(source_hash_stem: str) -> None:
            nonlocal convert_done
            convert_done += 1
            entry = by_hash.get(source_hash_stem)
            display_name = entry[0].path.name if entry else source_hash_stem
            self.sessionProgress.emit(session_id, PHASE_CONVERTING, convert_done, len(to_convert), display_name)

        tail = ""
        if to_convert:
            self._wait_if_paused()
            self.sessionProgress.emit(session_id, PHASE_CONVERTING, 0, len(to_convert), "")
            try:
                _returncode, tail = converter.convert_batch(
                    dnglab_path, staging_in, staging_out, compression, embed_raw, on_progress=on_converted,
                )
            except OSError as exc:
                for source_hash, cand, order in to_convert:
                    self._record_file(conn, session_id, cand.path.name, "failed", "", str(exc), order)
                to_convert = []

        bytes_saved = 0
        delete_originals = bool(settings.get("delete_originals_after_import"))
        placed = 0

        def place_file(cand: scanner.Candidate, order: int, source_hash: str, produced: Path,
                        move: bool) -> None:
            nonlocal placed, bytes_saved
            placed += 1
            self.sessionProgress.emit(session_id, PHASE_PLACING, placed, total_to_place, cand.path.name)
            dest = converter.unique_dest_path(converter.library_dest_path(library_root, cand))
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if move:
                    shutil.move(str(produced), str(dest))
                else:
                    shutil.copy2(str(produced), str(dest))
            except OSError as exc:
                self._record_file(conn, session_id, cand.path.name, "failed", "", str(exc), order)
                return

            converter.set_dng_backward_version(dest)
            dest_bytes = dest.stat().st_size
            now = datetime.now(timezone.utc).isoformat()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, "
                        "captured_at, dest_path, dest_bytes, session_id, imported_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (source_hash, cand.path.name, cand.size_bytes, cand.camera_model,
                         cand.captured_at, str(dest), dest_bytes, session_id, now),
                    )
            except sqlite3.IntegrityError:
                # Someone else already imported this exact content between
                # our dedup check and now (e.g. a second session racing on
                # the same source_hash -- see main.py's single-instance
                # lock for the case that used to make this common). Discard
                # the copy we just produced instead of leaving an orphan
                # duplicate file on disk, and record it like any other
                # dedup hit rather than crashing the whole session.
                dest.unlink(missing_ok=True)
                existing_dest = scanner.hash_duplicate_check(conn, source_hash) or ""
                self._record_file(conn, session_id, cand.path.name, "duplicate", existing_dest, "", order)
                return

            bytes_saved += max(cand.size_bytes - dest_bytes, 0)
            self._record_file(conn, session_id, cand.path.name, "imported", str(dest), "", order)

            if delete_originals:
                try:
                    cand.path.unlink()
                except OSError:
                    logger.info("Could not delete original %s after import", cand.path, exc_info=True)

        for source_hash, cand, order in to_convert:
            self._wait_if_paused()
            converted = staging_out / f"{source_hash}.dng"
            if not converted.is_file():
                placed += 1
                self.sessionProgress.emit(session_id, PHASE_PLACING, placed, total_to_place, cand.path.name)
                snippet = (tail or "conversion failed").strip()[-300:]
                self._record_file(conn, session_id, cand.path.name, "failed", "", snippet, order)
                continue
            place_file(cand, order, source_hash, converted, move=True)

        for source_hash, cand, order in to_copy:
            self._wait_if_paused()
            place_file(cand, order, source_hash, cand.path, move=False)

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
