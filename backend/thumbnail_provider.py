"""Serves `image://thumb/<url-encoded-absolute-path>` by extracting the
embedded JPEG preview from an ARW file via `exiftool -b -PreviewImage` --
fast (well under 100ms per file in testing) since it reads only the
embedded preview, never the raw sensor data. Used for the source picker's
thumbnail grid; QML's `Image { asynchronous: true }` runs this off the GUI
thread automatically."""
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
        path = unquote(id_)
        try:
            result = subprocess.run(
                ["exiftool", "-b", "-PreviewImage", path],
                capture_output=True, timeout=EXTRACT_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return QImage(), QSize()
        if result.returncode != 0 or not result.stdout:
            return QImage(), QSize()
        image = QImage.fromData(result.stdout)
        if image.isNull():
            return QImage(), QSize()
        if requested_size.isValid() and requested_size.width() > 0 and requested_size.height() > 0:
            image = image.scaled(
                requested_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
        return image, image.size()
