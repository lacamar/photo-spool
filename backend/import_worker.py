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
                "INSERT INTO sessions (started_at, device_label, source_root, kind, status, mark_only) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (started_at, request.device_label, request.source_root, request.kind, request.mark_only),
            )
        session_id = cur.lastrowid
        self.sessionStarted.emit(session_id)

        settings = settings_store.all_settings(conn)
        library_root = Path(settings["library_root"])

        try:
            files = scanner.find_importable_files(Path(request.source_root))
        except OSError as exc:
            self._finish_session(conn, session_id, "failed", error_message=str(exc))
            return

        with conn:
            conn.execute("UPDATE sessions SET found_count = ? WHERE id = ?", (len(files), session_id))
        self.sessionProgress.emit(session_id, PHASE_SCANNING, len(files), len(files), "")
        if not files:
            self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
            return

        # Read metadata (and, inside it, infer any missing camera_model --
        # see scanner._fill_missing_camera_models) over *every* file found
        # on the card, before narrowing down to a partial selection below.
        # A real, confirmed-live bug when this ran the other way around:
        # selecting just a couple of videos with no embedded Model tag (via
        # "Import N selected") gave inference nothing to borrow a model
        # from, so they got filed with a blank model segment in their
        # filename and a blank camera_model recorded in the ledger. The
        # *picker's* preview scan always reads the whole card, so it
        # infers the correct model for those same files -- meaning a later
        # quick_duplicate_match (keyed on camera_model among other things)
        # never found the mismatched ledger row, and an already-imported
        # file kept showing up as "new" indefinitely.
        metadata = scanner.read_metadata(files, conn)

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
                    error_message="The DNG converter (dnglab) isn't installed -- install the dnglab "
                                   "package, then retry from Settings.",
                )
                self.dnglabUnavailable.emit(session_id)
                return

        sort_order = 0
        # (source_hash, candidate, sort_order, staged_path). staged_path is
        # None for mark_only requests (nothing ever reads it again -- see
        # below), or the file's already-in-place local scratch copy for a
        # real import, produced by hashing and staging in the same read.
        to_stage: list[tuple[str, scanner.Candidate, int, Path | None]] = []

        # Staged once here, up front, so a slow source (an iPhone's AFC
        # mount, confirmed live: video imports took dramatically longer
        # than photo imports of a similar count) is only ever read across
        # that connection once -- whether the file then gets converted by
        # dnglab or copied through unchanged -- instead of once here to
        # hash it and again later to actually place it. mark_only never
        # places anything, so it keeps the plain read-only hash instead;
        # staging a local copy it'll never use would just waste local
        # disk I/O for no benefit.
        #
        # Two separate staging dirs, not one: dnglab's directory-mode
        # conversion processes *everything* it finds in its input dir, so
        # a DNG-passthrough/video file staged alongside real conversion
        # candidates would get pointlessly (and, for a large video,
        # expensively) run through dnglab too. staging_in is exclusively
        # dnglab's; staging_copy is exclusively for files headed straight
        # to place_file untouched.
        staging_root = paths.staging_dir() / f"session-{session_id}"
        staging_in = staging_root / "in"
        staging_copy = staging_root / "copy"
        if not request.mark_only:
            staging_in.mkdir(parents=True, exist_ok=True)
            staging_copy.mkdir(parents=True, exist_ok=True)

        # Hashes already queued in *this* batch (source_hash -> the filename
        # that claimed it first). The DB-level dedup check below can't see
        # these yet -- the earlier file hasn't been placed/recorded -- but
        # without this, two files with identical content in one batch (real
        # scenario: some cards/cameras keep more than one copy of the same
        # shot under different names) would both stage to the same
        # {hash}.ext path and both land in to_stage; the second one's
        # shutil.move later races the first for that same staged file (ENOENT
        # on the loser, wrongly reported as "failed") -- or, for a mark_only
        # batch, both INSERTs hit the imports.source_hash UNIQUE constraint,
        # the second raising an uncaught IntegrityError that killed the
        # whole session silently (left stuck in "running" forever, no error
        # ever surfaced). Recorded as "duplicate" once the primary's fate
        # (placed path, or nothing if it failed) is known -- see below.
        seen_hashes: set[str] = set()
        intra_batch_duplicates: list[tuple[str, str, int]] = []  # (source_hash, filename, sort_order)

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
                if request.mark_only:
                    source_hash = scanner.hash_file(cand.path)
                    staged_path = None
                else:
                    needs_conversion = not (scanner.is_dng(cand.path) or scanner.is_video(cand.path))
                    stage_dir = staging_in if needs_conversion else staging_copy
                    tmp_staged = stage_dir / f"{sort_order}{cand.path.suffix}"
                    source_hash = scanner.hash_and_stage(cand.path, tmp_staged)
                    staged_path = stage_dir / f"{source_hash}{cand.path.suffix}"
                    tmp_staged.replace(staged_path)
            except OSError as exc:
                self._record_file(conn, session_id, f.name, "failed", "", str(exc), sort_order)
                sort_order += 1
                continue
            existing_dest = scanner.hash_duplicate_check(conn, source_hash)
            if existing_dest is not None:
                if staged_path is not None:
                    staged_path.unlink(missing_ok=True)  # already-known content -- discard the wasted local copy
                self._record_file(conn, session_id, f.name, "duplicate", existing_dest, "", sort_order)
                sort_order += 1
                continue
            if source_hash in seen_hashes:
                # staged_path (if any) was just overwritten in-place with
                # identical bytes by the tmp_staged.replace() above -- the
                # primary's own staged copy at that same hash-named path is
                # untouched. Nothing to stage or clean up here.
                intra_batch_duplicates.append((source_hash, f.name, sort_order))
                sort_order += 1
                continue
            seen_hashes.add(source_hash)
            to_stage.append((source_hash, cand, sort_order, staged_path))
            sort_order += 1

        if not to_stage:
            self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
            return

        if request.mark_only:
            now = datetime.now(timezone.utc).isoformat()
            for source_hash, cand, order, _staged_path in to_stage:
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
            for source_hash, filename, order in intra_batch_duplicates:
                self._record_file(conn, session_id, filename, "duplicate", "", "", order)
            self._finish_session(conn, session_id, "completed", ejectable_path=request.source_root)
            return

        staging_out = staging_root / "out"

        # DNG sources (iPhone ProRAW, or a camera that already shoots DNG)
        # and videos need no conversion -- they're placed straight into the
        # library, keeping their own extension. Everything else gets
        # batch-converted by dnglab. Either way the file to use from here
        # on is already the local staged copy from the checking loop above
        # (named {hash}{suffix}, exactly what dnglab's directory mode
        # expects for matching a converted output back to its source) --
        # nothing here touches the original source again.
        to_convert: list[tuple[str, scanner.Candidate, int]] = []
        to_copy: list[tuple[str, scanner.Candidate, int, Path]] = []
        by_hash: dict[str, tuple[scanner.Candidate, int]] = {}
        for source_hash, cand, order, staged_path in to_stage:
            if scanner.is_dng(cand.path) or scanner.is_video(cand.path):
                to_copy.append((source_hash, cand, order, staged_path))
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
        # Filled in as each to_stage entry is placed, so any intra_batch_duplicates
        # sharing that source_hash can be recorded against the same real
        # destination once it's known (see intra_batch_duplicates comment above).
        placed_dest: dict[str, str] = {}

        def place_file(cand: scanner.Candidate, order: int, source_hash: str, produced: Path,
                        move: bool) -> None:
            nonlocal placed, bytes_saved
            placed += 1
            self.sessionProgress.emit(session_id, PHASE_PLACING, placed, total_to_place, cand.path.name)
            dest_suffix = produced.suffix.lower()
            dest = converter.unique_dest_path(converter.library_dest_path(library_root, cand, dest_suffix))
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if move:
                    shutil.move(str(produced), str(dest))
                else:
                    shutil.copy2(str(produced), str(dest))
            except OSError as exc:
                self._record_file(conn, session_id, cand.path.name, "failed", "", str(exc), order)
                return

            if dest_suffix == ".dng":
                converter.set_dng_backward_version(dest)
            elif cand.camera_model_inferred and scanner.is_video(dest):
                converter.set_camera_model(dest, cand.camera_model)
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
                placed_dest[source_hash] = existing_dest
                self._record_file(conn, session_id, cand.path.name, "duplicate", existing_dest, "", order)
                return

            bytes_saved += max(cand.size_bytes - dest_bytes, 0)
            placed_dest[source_hash] = str(dest)
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

        for source_hash, cand, order, staged_path in to_copy:
            self._wait_if_paused()
            place_file(cand, order, source_hash, staged_path, move=True)

        for source_hash, filename, order in intra_batch_duplicates:
            self._record_file(conn, session_id, filename, "duplicate", placed_dest.get(source_hash, ""), "", order)

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
