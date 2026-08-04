"""Resolves the `dnglab` binary (ARW -> DNG converter), downloading a
pinned prebuilt release from GitHub into our own data dir on first use if
it isn't already there. dnglab isn't packaged for Fedora; this is the
closest equivalent to how Lightroom's DNG conversion just works out of the
box. No sudo, no system-wide install -- everything lives under
`$XDG_DATA_HOME/photo-import/bin/`.
"""
from __future__ import annotations

import logging
import platform
import shutil
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from . import paths

logger = logging.getLogger(__name__)

DNGLAB_VERSION = "0.7.2"
RELEASE_BASE = f"https://github.com/dnglab/dnglab/releases/download/v{DNGLAB_VERSION}"
ASSET_BY_MACHINE = {
    "aarch64": "dnglab_linux_aarch64",
    "arm64": "dnglab_linux_aarch64",
    "x86_64": "dnglab_linux_x64",
    "amd64": "dnglab_linux_x64",
}
DOWNLOAD_TIMEOUT_S = 60


def _asset_name() -> str | None:
    return ASSET_BY_MACHINE.get(platform.machine().lower())


def bundled_path() -> Path:
    return paths.bin_dir() / "dnglab"


def _verify(path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_existing() -> Path | None:
    """Returns a working dnglab binary if one's already available -- our
    own downloaded copy, or one the user installed themselves on PATH --
    without touching the network."""
    bundled = bundled_path()
    if bundled.is_file() and _verify(bundled):
        return bundled
    on_path = shutil.which("dnglab")
    if on_path and _verify(Path(on_path)):
        return Path(on_path)
    return None


def download(progress_cb=None) -> Path | None:
    """Downloads the pinned release asset for this machine's architecture.
    Blocking -- call from a worker thread. Returns the verified path, or
    None if the architecture is unsupported or the download/verify failed
    (never raises; callers treat a missing converter as a normal,
    recoverable state, not a crash)."""
    asset = _asset_name()
    if asset is None:
        logger.warning("No prebuilt dnglab release for architecture %s", platform.machine())
        return None

    paths.ensure_dirs()
    dest = bundled_path()
    tmp = dest.with_suffix(".part")
    url = f"{RELEASE_BASE}/{asset}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "photo-import"})
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            written = 0
            with open(tmp, "wb") as fh:
                while chunk := resp.read(1024 * 256):
                    fh.write(chunk)
                    written += len(chunk)
                    if progress_cb is not None and total:
                        progress_cb(written, total)
        tmp.chmod(tmp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        tmp.replace(dest)
    except (urllib.error.URLError, OSError, TimeoutError):
        logger.warning("Failed to download dnglab", exc_info=True)
        tmp.unlink(missing_ok=True)
        return None

    if not _verify(dest):
        logger.warning("Downloaded dnglab binary failed to run")
        dest.unlink(missing_ok=True)
        return None
    return dest


def ensure(progress_cb=None) -> Path | None:
    """find_existing(), falling back to download() if nothing usable is
    already in place."""
    existing = find_existing()
    if existing is not None:
        return existing
    return download(progress_cb)


class EnsureWorker(QThread):
    """One-shot background resolve/download, so neither app startup nor a
    Settings-page "check now" click blocks the UI on a network call."""

    finishedOk = Signal(bool, str)  # success, resolved path (or empty on failure)

    def run(self) -> None:
        path = ensure()
        self.finishedOk.emit(path is not None, str(path) if path else "")
