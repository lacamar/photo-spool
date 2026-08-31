"""Typed key/value settings backed by the `settings` table."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "theme_mode": "system",  # light | dark | system
    # library_root deliberately isn't here -- it needs to read
    # XDG_PICTURES_DIR fresh on every lookup (see _default_for), not once
    # at module-import time. A module-level `paths.default_library_root()`
    # call here would bake in whatever XDG_PICTURES_DIR happened to be set
    # to the first time this module was ever imported in the process --
    # invisible in normal use (the env var never changes mid-run), but a
    # real hazard for anything that imports this module before setting up
    # an isolated environment (confirmed the hard way: an early version of
    # this project's test suite wrote real files into the real ~/Pictures
    # because of exactly this).
    "watch_enabled": True,  # auto-detect + import on card/camera insert
    "mtp_enabled": True,  # also watch for the camera plugged in over USB/MTP
    "delete_originals_after_import": False,
    "notify_on_complete": True,
    "dng_compression": "lossless",  # lossless | uncompressed
    "embed_raw_in_dng": False,  # off matches Lightroom's ~50% size reduction
    "preview_size": "medium",  # small | medium | full -- see converter.PREVIEW_FLAGS
}


def _default_for(key: str) -> Any:
    if key == "library_root":
        return str(paths.default_library_root())
    return DEFAULTS.get(key)


def get(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return _default_for(key)
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged["library_root"] = _default_for("library_root")
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            merged[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            merged[row["key"]] = row["value"]
    return merged
