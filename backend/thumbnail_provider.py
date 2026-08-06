"""Serves `image://thumb/<url-encoded-absolute-path>` for both the source-
picker grid and the session-detail file list. Raw/DNG files extract their
embedded JPEG preview via `exiftool -b -PreviewImage` -- fast (well under
100ms per file in testing) since it reads only the embedded preview, never
the raw sensor data. Video files have no such tag (confirmed empty against
real iPhone .MOV and camera .mp4 files), so those decode one real frame
with ffmpeg instead. QML's `Image { asynchronous: true }` runs this off the
GUI thread automatically."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from urllib.parse import unquote

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from . import scanner

logger = logging.getLogger(__name__)

EXTRACT_TIMEOUT_S = 10
VIDEO_FRAME_TIMEOUT_S = 15
# A fixed half-second offset skips an all-black opening frame on most
# clips; the "00:00:00" retry covers clips shorter than that (ffmpeg
# fails outright rather than clamping when -ss lands past the last frame).
VIDEO_FRAME_SEEK_OFFSETS = ("00:00:00.5", "00:00:00")


class ThumbnailImageProvider(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Image)

    def requestImage(self, id_: str, size: QSize, requested_size: QSize):
        # PySide6 binds C++'s `QImage requestImage(id, QSize *size, ...)`
        # by passing `size` in as a mutable out-param object, not as part
        # of the return value -- the only valid return is a bare QImage.
        # Returning a (QImage, QSize) tuple (as the C++/PyQt-style pointer
        # convention might suggest) fails silently from QML's perspective:
        # PySide logs a RuntimeWarning to stderr ("expected QImage, got
        # tuple") and every Image element using this provider just sits in
        # Image.Error state forever with no visible error in the UI.
        path = unquote(id_)
        raw = self._extract_video_frame(path) if scanner.is_video(Path(path)) else self._extract_preview(path)
        if raw is None:
            return QImage()
        image = QImage.fromData(raw)
        if image.isNull():
            return QImage()
        if requested_size.isValid() and requested_size.width() > 0 and requested_size.height() > 0:
            image = image.scaled(
                requested_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image

    def _extract_preview(self, path: str) -> bytes | None:
        try:
            result = subprocess.run(
                ["exiftool", "-b", "-PreviewImage", path],
                capture_output=True, timeout=EXTRACT_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        return result.stdout

    def _extract_video_frame(self, path: str) -> bytes | None:
        for seek in VIDEO_FRAME_SEEK_OFFSETS:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-nostdin", "-ss", seek, "-i", path, "-frames:v", "1",
                     "-vf", "scale=320:-1", "-f", "mjpeg", "pipe:1"],
                    capture_output=True, timeout=VIDEO_FRAME_TIMEOUT_S,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode == 0 and result.stdout:
                return result.stdout
        return None
