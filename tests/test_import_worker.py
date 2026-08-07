"""Exercises ImportWorker._run_session directly and synchronously (never
via .start()/QThread) against synthetic local files -- no real hardware,
no real dnglab download. _run_session is a plain method; nothing here
requires the Qt event loop to actually be running, only a QCoreApplication
instance to exist (PySide6's signal/slot machinery needs one)."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication

from backend import db, scanner
from backend.import_worker import ImportRequest, ImportWorker
from tests.testutil import IsolatedTestCase

if QCoreApplication.instance() is None:
    _app = QCoreApplication([])


def _fake_convert_batch(dnglab_path, staging_in, staging_out, compression, embed_raw, on_progress=None):
    """Stands in for a real dnglab invocation: copies each staged input
    straight to <hash>.dng in staging_out, so the rest of the pipeline
    (matching, placement, DNGBackwardVersion) can be exercised without a
    real dnglab binary or a real decodable raw file."""
    staging_out.mkdir(parents=True, exist_ok=True)
    for src in staging_in.iterdir():
        dest = staging_out / f"{src.stem}.dng"
        shutil.copy2(src, dest)
        if on_progress is not None:
            on_progress(src.stem)
    return 0, ""


class ImportWorkerTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect()
        self.worker = ImportWorker()
        self.source_root = self.tmp / "card"
        self.source_root.mkdir()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _run(self, **kwargs) -> int:
        request = ImportRequest(source_root=str(self.source_root), device_label="Test", kind="blockdev", **kwargs)
        # dnglab_setup.ensure() falls back to a real network download if no
        # binary is already resolvable -- never let a test touch the
        # network, regardless of what's installed on the machine running it.
        with mock.patch("backend.import_worker.converter.convert_batch", side_effect=_fake_convert_batch), \
             mock.patch("backend.import_worker.dnglab_setup.ensure", return_value=Path("/fake/dnglab")):
            self.worker._run_session(self.conn, request)
        return self.conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]

    def _session(self, session_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())


class VideoPassthroughTests(ImportWorkerTestCase):
    """Covers the actual bug report: video imports from an iPhone hanging
    much longer than photo imports, root-caused to every file being fully
    read twice (once to hash it, again to place it) over a slow AFC
    connection."""

    def _write_video(self, name="clip.mov", content=b"fake video bytes" * 10000) -> Path:
        f = self.source_root / name
        f.write_bytes(content)
        return f

    def test_video_is_placed_with_correct_content(self):
        f = self._write_video()
        session_id = self._run()
        row = self._session(session_id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["imported_count"], 1)
        self.assertEqual(row["failed_count"], 0)

        dest_row = self.conn.execute(
            "SELECT dest_path FROM session_files WHERE session_id = ? AND status = 'imported'", (session_id,)
        ).fetchone()
        dest = Path(dest_row["dest_path"])
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.suffix, ".mov")
        self.assertEqual(dest.read_bytes(), f.read_bytes())

    def test_source_file_is_only_ever_read_once(self):
        # The whole point of the fix: shutil.copy2 (a second full read of
        # the *original* source) must never be called for a passthrough
        # file -- placement uses the already-staged local copy via a move
        # instead. And scanner.hash_file (the old read-only-no-stage path)
        # must never be used for a real import -- only hash_and_stage.
        self._write_video()
        with mock.patch("backend.import_worker.shutil.copy2") as mock_copy2, \
             mock.patch("backend.import_worker.scanner.hash_file") as mock_hash_file:
            self._run()
            mock_copy2.assert_not_called()
            mock_hash_file.assert_not_called()

    def test_dng_passthrough_also_uses_staged_copy(self):
        f = self.source_root / "IMG_0001.dng"
        f.write_bytes(b"fake dng bytes" * 10000)
        with mock.patch("backend.import_worker.shutil.copy2") as mock_copy2:
            session_id = self._run()
            mock_copy2.assert_not_called()
        self.assertEqual(self._session(session_id)["imported_count"], 1)

    def test_reimporting_the_same_video_is_a_duplicate(self):
        self._write_video()
        first = self._run()
        self.assertEqual(self._session(first)["imported_count"], 1)

        second = self._run()
        row = self._session(second)
        self.assertEqual(row["imported_count"], 0)
        self.assertEqual(row["duplicate_count"], 1)

    def test_staging_dirs_are_cleaned_up_after_the_session(self):
        self._write_video()
        self._run()
        staging_root = list((self.tmp / "data" / "photo-import" / "staging").iterdir()) \
            if (self.tmp / "data" / "photo-import" / "staging").is_dir() else []
        self.assertEqual(staging_root, [])


class ConversionRoutingTests(ImportWorkerTestCase):
    """The bug I caught in my own draft: dnglab's directory-mode
    conversion processes *everything* in its input dir, so a video/DNG
    file staged alongside real conversion candidates would get pointlessly
    (and, for a large video, expensively) run through dnglab too."""

    def test_staging_in_only_ever_contains_conversion_candidates(self):
        (self.source_root / "DSC00001.ARW").write_bytes(b"fake raw bytes" * 10000)
        (self.source_root / "clip.mov").write_bytes(b"fake video bytes" * 10000)
        seen_in_staging_in = []

        def spy_convert_batch(dnglab_path, staging_in, staging_out, compression, embed_raw, on_progress=None):
            seen_in_staging_in.extend(p.name for p in staging_in.iterdir())
            return _fake_convert_batch(dnglab_path, staging_in, staging_out, compression, embed_raw, on_progress)

        request = ImportRequest(source_root=str(self.source_root), device_label="Test", kind="blockdev")
        with mock.patch("backend.import_worker.converter.convert_batch", side_effect=spy_convert_batch), \
             mock.patch("backend.import_worker.dnglab_setup.ensure", return_value=Path("/fake/dnglab")):
            self.worker._run_session(self.conn, request)

        self.assertEqual(len(seen_in_staging_in), 1)
        self.assertTrue(seen_in_staging_in[0].endswith(".ARW"))

    def test_converted_file_is_placed_as_dng_with_backward_version_set(self):
        (self.source_root / "DSC00001.ARW").write_bytes(b"fake raw bytes" * 10000)
        with mock.patch("backend.import_worker.converter.set_dng_backward_version") as mock_set_version:
            session_id = self._run()
            mock_set_version.assert_called_once()
        row = self._session(session_id)
        self.assertEqual(row["imported_count"], 1)
        dest_row = self.conn.execute(
            "SELECT dest_path FROM session_files WHERE session_id = ? AND status = 'imported'", (session_id,)
        ).fetchone()
        self.assertTrue(dest_row["dest_path"].endswith(".dng"))


class MarkOnlyTests(ImportWorkerTestCase):
    def test_mark_only_records_ledger_entry_without_placing_anything(self):
        f = self.source_root / "clip.mov"
        f.write_bytes(b"fake video bytes" * 10000)
        session_id = self._run(mark_only=True, selected_filenames=frozenset({"clip.mov"}))

        row = self._session(session_id)
        self.assertTrue(row["mark_only"])
        self.assertEqual(row["imported_count"], 1)

        ledger_row = self.conn.execute("SELECT dest_path FROM imports WHERE source_filename = 'clip.mov'").fetchone()
        self.assertEqual(ledger_row["dest_path"], "")
        # The original file must be untouched -- mark_only never places anything.
        self.assertTrue(f.is_file())

    def test_mark_only_never_creates_staging_directories(self):
        f = self.source_root / "clip.mov"
        f.write_bytes(b"fake video bytes" * 10000)
        self._run(mark_only=True, selected_filenames=frozenset({"clip.mov"}))
        staging_dir = self.tmp / "data" / "photo-import" / "staging"
        if staging_dir.is_dir():
            for session_dir in staging_dir.iterdir():
                self.assertFalse((session_dir / "in").exists())
                self.assertFalse((session_dir / "copy").exists())

    def test_mark_only_uses_plain_hash_not_staging(self):
        f = self.source_root / "clip.mov"
        f.write_bytes(b"fake video bytes" * 10000)
        with mock.patch("backend.import_worker.scanner.hash_and_stage") as mock_stage:
            self._run(mark_only=True, selected_filenames=frozenset({"clip.mov"}))
            mock_stage.assert_not_called()


class IntraBatchDuplicateContentTests(ImportWorkerTestCase):
    """Two source files with byte-identical content but different names in
    the *same* batch -- confirmed possible in the wild (some cards/cameras
    keep more than one copy of an identical shot). Before this fix: for a
    real import, both staged to the same {hash}.ext path and both entered
    the placement loop, so the second's shutil.move raced the first's
    already-moved-away file and got misreported as "failed" with a
    confusing ENOENT message; for mark_only, both INSERTs targeted the same
    UNIQUE source_hash and the second raised an uncaught IntegrityError that
    killed the whole session silently (stuck at status "running" forever)."""

    def test_duplicate_content_video_recorded_as_duplicate_not_failed(self):
        content = b"identical video bytes" * 10000
        (self.source_root / "a.mov").write_bytes(content)
        (self.source_root / "b.mov").write_bytes(content)

        session_id = self._run()
        row = self._session(session_id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["imported_count"], 1)
        self.assertEqual(row["duplicate_count"], 1)
        self.assertEqual(row["failed_count"], 0)

        statuses = {
            r["source_filename"]: r["status"]
            for r in self.conn.execute(
                "SELECT source_filename, status FROM session_files WHERE session_id = ?", (session_id,)
            )
        }
        self.assertEqual(statuses, {"a.mov": "imported", "b.mov": "duplicate"})

    def test_duplicate_content_video_dest_path_points_at_the_placed_file(self):
        content = b"identical video bytes" * 10000
        (self.source_root / "a.mov").write_bytes(content)
        (self.source_root / "b.mov").write_bytes(content)

        session_id = self._run()
        imported_dest = self.conn.execute(
            "SELECT dest_path FROM session_files WHERE session_id = ? AND source_filename = 'a.mov'", (session_id,)
        ).fetchone()["dest_path"]
        duplicate_dest = self.conn.execute(
            "SELECT dest_path FROM session_files WHERE session_id = ? AND source_filename = 'b.mov'", (session_id,)
        ).fetchone()["dest_path"]
        self.assertTrue(imported_dest)
        self.assertEqual(imported_dest, duplicate_dest)
        # Only one row in the ledger -- source_hash is UNIQUE, and this
        # content was only ever placed once.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM imports WHERE source_hash IS NOT NULL").fetchone()["n"], 1
        )

    def test_duplicate_content_raw_converted_once_not_twice(self):
        content = b"identical raw bytes" * 10000
        (self.source_root / "DSC00001.ARW").write_bytes(content)
        (self.source_root / "DSC00002.ARW").write_bytes(content)
        convert_calls = []

        def spy_convert_batch(dnglab_path, staging_in, staging_out, compression, embed_raw, on_progress=None):
            convert_calls.append([p.name for p in staging_in.iterdir()])
            return _fake_convert_batch(dnglab_path, staging_in, staging_out, compression, embed_raw, on_progress)

        request = ImportRequest(source_root=str(self.source_root), device_label="Test", kind="blockdev")
        with mock.patch("backend.import_worker.converter.convert_batch", side_effect=spy_convert_batch), \
             mock.patch("backend.import_worker.dnglab_setup.ensure", return_value=Path("/fake/dnglab")):
            self.worker._run_session(self.conn, request)

        # Only one physical file ever reached dnglab for this one unique hash.
        self.assertEqual(len(convert_calls[0]), 1)
        session_id = self.conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]
        row = self._session(session_id)
        self.assertEqual(row["imported_count"], 1)
        self.assertEqual(row["duplicate_count"], 1)
        self.assertEqual(row["failed_count"], 0)

    def test_mark_only_duplicate_content_does_not_crash_the_session(self):
        content = b"identical video bytes" * 10000
        (self.source_root / "a.mov").write_bytes(content)
        (self.source_root / "b.mov").write_bytes(content)

        session_id = self._run(mark_only=True)
        row = self._session(session_id)
        self.assertEqual(row["status"], "completed")  # not stuck at "running"
        self.assertEqual(row["imported_count"], 1)
        self.assertEqual(row["duplicate_count"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM imports").fetchone()["n"], 1
        )


class SelectedFilenamesTests(ImportWorkerTestCase):
    def test_only_selected_files_are_processed(self):
        (self.source_root / "a.mov").write_bytes(b"a" * 1000)
        (self.source_root / "b.mov").write_bytes(b"b" * 1000)
        session_id = self._run(selected_filenames=frozenset({"a.mov"}))
        row = self._session(session_id)
        self.assertEqual(row["found_count"], 2)  # both were found by the scan...
        self.assertEqual(row["imported_count"], 1)  # ...but only the selected one was processed
