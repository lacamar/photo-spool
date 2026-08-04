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


def db_path() -> Path:
    return data_home() / "data.db"


def bin_dir() -> Path:
    return data_home() / "bin"


def staging_dir() -> Path:
    """Scratch area for symlinked ARW inputs and dnglab's DNG outputs during
    a single import run. Cleared at the start of every run; never relied on
    to persist anything -- the DB is the source of truth."""
    return data_home() / "staging"


def default_library_root() -> Path:
    xdg = os.environ.get("XDG_PICTURES_DIR")
    return Path(xdg) if xdg else Path.home() / "Pictures"


def ensure_dirs() -> None:
    data_home().mkdir(parents=True, exist_ok=True)
    bin_dir().mkdir(parents=True, exist_ok=True)
