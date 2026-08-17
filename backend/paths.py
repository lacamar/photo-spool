"""XDG path resolution for the app's data directory, DB file, downloaded
dnglab binary, and import staging area."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "photo-spool"
_OLD_APP_NAME = "photo-import"  # pre-rename directory name -- see _migrate_data_home


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
    any time (a cache miss just re-extracts, same as today). The "v2"
    component is a cache *format* version, bumped when what gets written
    here changes shape -- v1 cached the raw, unshrunk extraction (up to
    10+MB per entry, 3.6GB total for one real device's camera roll,
    confirmed live), v2 shrinks to a real thumbnail size before writing.
    Versioning the directory lets old-format entries be abandoned
    outright instead of needing to be detected and migrated; bump this
    again the next time the cached format changes."""
    return cache_home() / "thumbnails" / "v2"


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


def _migrate_data_home() -> None:
    """One-time move of the pre-rename data directory (DB, settings, the
    self-downloaded dnglab binary) from ~/.local/share/photo-import to
    ~/.local/share/photo-spool, so the app-name rename doesn't silently
    orphan real session history behind a path nothing reads anymore. Only
    acts when the new dir doesn't exist yet and the old one does -- once
    migrated (or on a fresh install with no old dir), this is a no-op
    forever. A plain os.rename, not a merge: nothing else in this app has
    ever written to old_home post-rename, so there's nothing to merge."""
    new_home = data_home()
    if new_home.exists():
        return
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    old_home = base / _OLD_APP_NAME
    if not old_home.exists():
        return
    new_home.parent.mkdir(parents=True, exist_ok=True)
    try:
        old_home.rename(new_home)
    except OSError:
        pass  # cross-filesystem XDG_DATA_HOME override, permissions, etc. -- degrade to a fresh data dir


def ensure_dirs() -> None:
    _migrate_data_home()
    data_home().mkdir(parents=True, exist_ok=True)
    bin_dir().mkdir(parents=True, exist_ok=True)
