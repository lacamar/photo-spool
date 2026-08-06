"""XDG path resolution for the app's data directory, DB file, downloaded
dnglab binary, and import staging area."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "photo-import"


def data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


def cache_home() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / APP_NAME


def thumbnail_cache_dir() -> Path:
    """Extracted-preview cache for ThumbnailImageProvider -- keyed by
    (path, mtime, size), so it's disposable and safe to wipe entirely at
    any time (a cache miss just re-extracts, same as today)."""
    return cache_home() / "thumbnails"


def db_path() -> Path:
    return data_home() / "data.db"


def bin_dir() -> Path:
    return data_home() / "bin"


def staging_dir() -> Path:
    """Scratch area for locally-staged copies of source files (staged
    while hashing them, so a slow source only needs one read -- see
    scanner.hash_and_stage) and dnglab's DNG outputs, during a single
    import run. Cleared at the start of every run; never relied on to
    persist anything -- the DB is the source of truth. Temporarily uses
    real local disk space roughly equal to the total size of everything
    new being imported in that run, not just symlinks as the name might
    suggest from its git history."""
    return data_home() / "staging"


def default_library_root() -> Path:
    xdg = os.environ.get("XDG_PICTURES_DIR")
    return Path(xdg) if xdg else Path.home() / "Pictures"


def ensure_dirs() -> None:
    data_home().mkdir(parents=True, exist_ok=True)
    bin_dir().mkdir(parents=True, exist_ok=True)
