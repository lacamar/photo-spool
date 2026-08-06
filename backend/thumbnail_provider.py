"""Serves `image://thumb/<url-encoded-absolute-path>` by extracting the
embedded JPEG preview from a raw/DNG file via `exiftool -b -PreviewImage` --
fast (well under 100ms per file in testing) since it reads only the
embedded preview, never the raw sensor data. Used for the source picker's
thumbnail grid; QML's `Image { asynchronous: true }` runs this off the GUI
thread automatically. Video files have no such tag, so this comes back
empty for them -- callers just fall back to their placeholder tile, which
is fine since a thumbnail was never the point for a video."""
from __future__ import annotations

import logging
import subprocess
from urllib.parse import unquote

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

logger = logging.getLogger(__name__)

EXTRACT_TIMEOUT_S = 10


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
        try:
            result = subprocess.run(
                ["exiftool", "-b", "-PreviewImage", path],
                capture_output=True, timeout=EXTRACT_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return QImage()
        if result.returncode != 0 or not result.stdout:
            return QImage()
        image = QImage.fromData(result.stdout)
        if image.isNull():
            return QImage()
        if requested_size.isValid() and requested_size.width() > 0 and requested_size.height() > 0:
            image = image.scaled(
                requested_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image
