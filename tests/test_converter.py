from __future__ import annotations

import subprocess
import unittest
from datetime import date
from pathlib import Path

from backend import converter, scanner
from tests.testutil import IsolatedTestCase


def _has_exiftool() -> bool:
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class LibraryDestPathTests(IsolatedTestCase):
    def _candidate(self, path: Path, captured_at: str | None, model="ILCE-7RM3") -> scanner.Candidate:
        return scanner.Candidate(path=path, size_bytes=100, camera_model=model, captured_at=captured_at)

    def test_naming_convention_with_captured_at(self):
        cand = self._candidate(Path("DSC01075.ARW"), "2026-05-24T10:00:00")
        dest = converter.library_dest_path(self.tmp / "lib", cand, ".dng")
        self.assertEqual(
            dest,
            self.tmp / "lib" / "2026" / "2026-05" / "2026-05-24" / "2026.05.24_ILCE-7RM3_01075.dng",
        )

    def test_suffix_is_used_verbatim_for_passthrough(self):
        cand = self._candidate(Path("IMG_0002.MOV"), "2026-05-24T10:00:00")
        dest = converter.library_dest_path(self.tmp / "lib", cand, ".mov")
        self.assertEqual(dest.suffix, ".mov")
        self.assertEqual(dest.name, "2026.05.24_ILCE-7RM3_0002.mov")

    def test_falls_back_to_mtime_when_no_captured_at(self):
        f = self.tmp / "src.arw"
        f.write_bytes(b"x")
        cand = self._candidate(f, None)
        dest = converter.library_dest_path(self.tmp / "lib", cand, ".dng")
        expected_date = date.fromtimestamp(f.stat().st_mtime)
        self.assertIn(str(expected_date.year), str(dest))

    def test_falls_back_to_mtime_for_an_invalid_captured_at_instead_of_crashing(self):
        # Belt-and-suspenders for a real crash: scanner._parse_exif_datetime
        # is supposed to reject this before it ever gets here, but this
        # exact line (date.fromisoformat on a bogus string) is what
        # actually crashed the whole import session live, so it must never
        # be able to happen again even if a Candidate is constructed some
        # other way with already-bad data.
        f = self.tmp / "src.mov"
        f.write_bytes(b"x")
        cand = self._candidate(f, "0000-00-00T00:00:00")
        dest = converter.library_dest_path(self.tmp / "lib", cand, ".mov")
        expected_date = date.fromtimestamp(f.stat().st_mtime)
        self.assertIn(str(expected_date.year), str(dest))

    def test_unsafe_characters_stripped_from_model(self):
        cand = self._candidate(Path("a.arw"), "2026-05-24T10:00:00", model="Weird/Model Name!")
        dest = converter.library_dest_path(self.tmp / "lib", cand, ".dng")
        self.assertNotIn("/", dest.name)
        self.assertNotIn("!", dest.name)


class UniqueDestPathTests(IsolatedTestCase):
    def test_no_collision_returns_same_path(self):
        p = self.tmp / "a.dng"
        self.assertEqual(converter.unique_dest_path(p), p)

    def test_collision_appends_counter(self):
        p = self.tmp / "a.dng"
        p.write_bytes(b"x")
        result = converter.unique_dest_path(p)
        self.assertEqual(result, self.tmp / "a (2).dng")

    def test_multiple_collisions_increment(self):
        (self.tmp / "a.dng").write_bytes(b"x")
        (self.tmp / "a (2).dng").write_bytes(b"x")
        result = converter.unique_dest_path(self.tmp / "a.dng")
        self.assertEqual(result, self.tmp / "a (3).dng")


@unittest.skipUnless(_has_exiftool(), "exiftool not available")
class ExiftoolWriteTests(IsolatedTestCase):
    def _sample_video(self) -> Path:
        # A minimal but real MP4 container -- ffmpeg is a hard Requires
        # for this app, so generating a tiny fixture with it is safe to
        # assume works wherever this test suite runs.
        f = self.tmp / "sample.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=1", "-frames:v", "1", str(f)],
            capture_output=True, timeout=15, check=True,
        )
        return f

    def test_set_camera_model_writes_tag(self):
        f = self._sample_video()
        converter.set_camera_model(f, "ILCE-7RM3")
        result = subprocess.run(["exiftool", "-s3", "-Model", str(f)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), "ILCE-7RM3")

    def test_set_camera_model_nonexistent_file_does_not_raise(self):
        converter.set_camera_model(self.tmp / "nope.mp4", "ILCE-7RM3")  # should just log, not raise
