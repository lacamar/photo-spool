"""QImage/QBuffer operations here work without any QCoreApplication
instantiated (confirmed: unlike QPixmap, QImage doesn't need a GUI
context) -- no PySide6 application scaffolding needed for these tests."""
from __future__ import annotations

from PySide6.QtCore import QBuffer, QIODeviceBase
from PySide6.QtGui import QImage

from backend.thumbnail_provider import CACHE_MAX_DIMENSION, ThumbnailImageProvider, _cache_path
from tests.testutil import IsolatedTestCase


def _jpeg_bytes(width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF0000)
    buffer = QBuffer()
    buffer.open(QIODeviceBase.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPEG", 90)
    return bytes(buffer.data())


class ShrinkTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.provider = ThumbnailImageProvider()

    def test_large_image_is_shrunk_to_max_dimension(self):
        # A real embedded RAW preview came in over 10MB, confirmed live
        # against this machine's own library -- nothing in this app ever
        # displays a thumbnail larger than ~200px, so caching that
        # unshrunk wastes enormous disk space and makes every cache *hit*
        # still slow to decode.
        raw = _jpeg_bytes(2000, 1500)
        shrunk = self.provider._shrink(raw)

        self.assertLess(len(shrunk), len(raw))
        image = QImage.fromData(shrunk)
        self.assertLessEqual(image.width(), CACHE_MAX_DIMENSION)
        self.assertLessEqual(image.height(), CACHE_MAX_DIMENSION)
        # Aspect ratio preserved (4:3 in, 4:3 out).
        self.assertEqual(image.width(), CACHE_MAX_DIMENSION)
        self.assertEqual(image.height(), 240)

    def test_already_small_image_is_left_untouched(self):
        raw = _jpeg_bytes(100, 80)
        self.assertEqual(self.provider._shrink(raw), raw)

    def test_undecodable_bytes_are_returned_as_is(self):
        garbage = b"not an image"
        self.assertEqual(self.provider._shrink(garbage), garbage)


class CachePathTests(IsolatedTestCase):
    def test_stable_for_the_same_file(self):
        f = self.tmp / "photo.dng"
        f.write_bytes(b"x" * 100)
        self.assertEqual(_cache_path(str(f)), _cache_path(str(f)))

    def test_differs_when_mtime_or_size_changes(self):
        f = self.tmp / "photo.dng"
        f.write_bytes(b"x" * 100)
        original = _cache_path(str(f))

        f.write_bytes(b"y" * 200)  # different size and mtime
        self.assertNotEqual(_cache_path(str(f)), original)

    def test_none_for_a_nonexistent_path(self):
        self.assertIsNone(_cache_path(str(self.tmp / "does-not-exist.dng")))


class CacheRoundtripTests(IsolatedTestCase):
    def test_extract_and_cache_end_to_end(self):
        provider = ThumbnailImageProvider()
        raw = _jpeg_bytes(2000, 1500)
        provider._extract_preview = lambda path: raw  # avoid needing a real exiftool-decodable file

        f = self.tmp / "photo.dng"
        f.write_bytes(b"fake dng bytes")
        cache_path = _cache_path(str(f))
        self.assertFalse(cache_path.exists())

        first = provider._load_or_extract(str(f))
        self.assertTrue(cache_path.is_file())
        cached_size = cache_path.stat().st_size
        self.assertLess(cached_size, len(raw))  # shrunk before being written, not just before display

        second = provider._load_or_extract(str(f))
        self.assertEqual(first, second)
