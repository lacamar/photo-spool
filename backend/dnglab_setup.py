"""Locates the `dnglab` binary (ARW -> DNG converter). dnglab is packaged
as a system RPM (see the photo-spool spec's `Requires: dnglab`) and is
expected on PATH; this module no longer downloads or manages a private
copy of it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _verify(path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_existing() -> Path | None:
    """Returns a working dnglab binary if one's on PATH, without touching
    the network -- there's nothing left to download."""
    on_path = shutil.which("dnglab")
    if on_path and _verify(Path(on_path)):
        return Path(on_path)
    return None


def ensure() -> Path | None:
    """Alias for find_existing(), kept so callers don't need to care that
    there's no longer a fallback/download step."""
    return find_existing()
