from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from backend import scanner
from tests.testutil import IsolatedTestCase


class ExtensionDetectionTests(IsolatedTestCase):
    def test_is_dng(self):
        self.assertTrue(scanner.is_dng(Path("a/b.dng")))
        self.assertTrue(scanner.is_dng(Path("a/b.DNG")))
        self.assertFalse(scanner.is_dng(Path("a/b.arw")))

    def test_is_video(self):
        for ext in (".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi", ".MOV"):
            self.assertTrue(scanner.is_video(Path("x" + ext)), ext)
        for ext in (".arw", ".dng", ".jpg", ".txt"):
            self.assertFalse(scanner.is_video(Path("x" + ext)), ext)

    def test_find_importable_files_filters_by_extension_and_hidden(self):
        root = self.tmp / "card"
        root.mkdir()
        (root / "DSC00001.ARW").write_bytes(b"a")
        (root / "clip.mp4").write_bytes(b"b")
        (root / "photo.jpg").write_bytes(b"c")  # not raw/video -- excluded
        (root / ".hidden.arw").write_bytes(b"d")  # dotfile -- excluded
        sub = root / "sub"
        sub.mkdir()
        (sub / "nested.dng").write_bytes(b"e")

        found = {p.name for p in scanner.find_importable_files(root)}
        self.assertEqual(found, {"DSC00001.ARW", "clip.mp4", "nested.dng"})


class ShotNumberTests(IsolatedTestCase):
    def test_trailing_digits(self):
        self.assertEqual(scanner.shot_number("DSC01075.ARW"), "01075")
        self.assertEqual(scanner.shot_number("IMG_0002.MOV"), "0002")

    def test_no_trailing_digits_falls_back_to_stem(self):
        self.assertEqual(scanner.shot_number("no_numbers_here.arw"), "no_numbers_here")


class ExifDatetimeParsingTests(IsolatedTestCase):
    def test_valid(self):
        self.assertEqual(scanner._parse_exif_datetime("2026:05:24 10:00:00"), "2026-05-24T10:00:00")

    def test_invalid_or_missing(self):
        self.assertIsNone(scanner._parse_exif_datetime(None))
        self.assertIsNone(scanner._parse_exif_datetime(""))
        self.assertIsNone(scanner._parse_exif_datetime("not a date"))
        self.assertIsNone(scanner._parse_exif_datetime(0))

    def test_rejects_regex_shaped_but_impossible_dates(self):
        # A real crash, confirmed live against a real iPhone video: some
        # cameras/video files write an all-zero placeholder timestamp when
        # they never actually recorded one. The old regex-only check
        # accepted "0000-00-00T00:00:00" as a valid-*looking* string, which
        # then crashed three call frames downstream in
        # converter.library_dest_path (date.fromisoformat: "year must be
        # in 1..9999, not 0") -- silently killing the whole import
        # session's worker thread with no error ever surfaced to the user.
        self.assertIsNone(scanner._parse_exif_datetime("0000:00:00 00:00:00"))
        self.assertIsNone(scanner._parse_exif_datetime("2026:02:30 10:00:00"))  # Feb 30th doesn't exist
        self.assertIsNone(scanner._parse_exif_datetime("2026:13:01 10:00:00"))  # month 13
        self.assertIsNone(scanner._parse_exif_datetime("2026:01:01 25:00:00"))  # hour 25


class FillMissingCameraModelsTests(IsolatedTestCase):
    def _candidate(self, name: str, model: str) -> scanner.Candidate:
        return scanner.Candidate(path=Path(name), size_bytes=100, camera_model=model, captured_at=None)

    def test_unambiguous_batch_fills_blank_model(self):
        candidates = {
            Path("a"): self._candidate("a", "ILCE-7RM3"),
            Path("b"): self._candidate("b", "ILCE-7RM3"),
            Path("c"): self._candidate("c", ""),  # e.g. a video with no embedded Model
        }
        scanner._fill_missing_camera_models(candidates)
        c = candidates[Path("c")]
        self.assertEqual(c.camera_model, "ILCE-7RM3")
        self.assertTrue(c.camera_model_inferred)
        # Files that already had a model are untouched.
        self.assertFalse(candidates[Path("a")].camera_model_inferred)

    def test_ambiguous_batch_does_not_guess(self):
        candidates = {
            Path("a"): self._candidate("a", "ILCE-7RM3"),
            Path("b"): self._candidate("b", "iPhone 14"),
            Path("c"): self._candidate("c", ""),
        }
        scanner._fill_missing_camera_models(candidates)
        self.assertEqual(candidates[Path("c")].camera_model, "")
        self.assertFalse(candidates[Path("c")].camera_model_inferred)

    def test_no_known_model_in_batch_does_not_guess(self):
        candidates = {
            Path("a"): self._candidate("a", ""),
            Path("b"): self._candidate("b", ""),
        }
        scanner._fill_missing_camera_models(candidates)
        self.assertEqual(candidates[Path("a")].camera_model, "")
        self.assertEqual(candidates[Path("b")].camera_model, "")


class HashingTests(IsolatedTestCase):
    def test_hash_file_matches_hashlib(self):
        f = self.tmp / "x.bin"
        content = b"some file content" * 1000
        f.write_bytes(content)
        self.assertEqual(scanner.hash_file(f), hashlib.sha256(content).hexdigest())

    def test_hash_and_stage_matches_hash_file_and_copies_content(self):
        src = self.tmp / "src.bin"
        content = b"video-like bytes" * 5000
        src.write_bytes(content)
        dest = self.tmp / "staged" / "out.bin"
        dest.parent.mkdir()

        staged_hash = scanner.hash_and_stage(src, dest)

        self.assertEqual(staged_hash, scanner.hash_file(src))
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), content)

    def test_hash_and_stage_cleans_up_dest_on_read_failure(self):
        dest = self.tmp / "staged" / "out.bin"
        dest.parent.mkdir()
        with self.assertRaises(OSError):
            scanner.hash_and_stage(self.tmp / "does-not-exist.bin", dest)
        self.assertFalse(dest.exists())


class DedupLookupTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        from backend import db
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_quick_match_and_hash_check_roundtrip(self):
        self.assertIsNone(scanner.quick_duplicate_match(self.conn, "ILCE-7RM3", "a.arw", 100))
        self.assertIsNone(scanner.hash_duplicate_check(self.conn, "deadbeef"))

        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions (started_at, device_label, source_root, kind, status) "
                "VALUES ('t', 'SD', '/root', 'blockdev', 'completed')"
            )
            self.conn.execute(
                "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, "
                "captured_at, dest_path, dest_bytes, session_id, imported_at) "
                "VALUES ('deadbeef', 'a.arw', 100, 'ILCE-7RM3', 't', '/lib/a.dng', 90, 1, 't')"
            )

        self.assertEqual(
            scanner.quick_duplicate_match(self.conn, "ILCE-7RM3", "a.arw", 100), "/lib/a.dng",
        )
        self.assertEqual(scanner.hash_duplicate_check(self.conn, "deadbeef"), "/lib/a.dng")
        # A different filename/size never quick-matches even for the same model.
        self.assertIsNone(scanner.quick_duplicate_match(self.conn, "ILCE-7RM3", "b.arw", 100))

    def test_removing_a_ledger_entry_makes_it_show_as_new_again(self):
        # Mirrors AppController.unmarkImported's DELETE -- the whole point
        # of "unmark" is that this needs no hashing, just the same
        # (camera_model, filename, size) key quick_duplicate_match uses.
        with self.conn:
            self.conn.execute(
                "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, "
                "captured_at, dest_path, dest_bytes, session_id, imported_at) "
                "VALUES ('h', 'a.mov', 100, 'ILCE-7RM3', NULL, '', 0, NULL, 't')"
            )
        self.assertIsNotNone(scanner.quick_duplicate_match(self.conn, "ILCE-7RM3", "a.mov", 100))

        with self.conn:
            self.conn.execute(
                "DELETE FROM imports WHERE camera_model = ? AND source_filename = ? AND source_bytes = ?",
                ("ILCE-7RM3", "a.mov", 100),
            )

        self.assertIsNone(scanner.quick_duplicate_match(self.conn, "ILCE-7RM3", "a.mov", 100))
