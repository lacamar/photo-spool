"""Typed key/value settings backed by the `settings` table."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "theme_mode": "system",  # light | dark | system
    "library_root": str(paths.default_library_root()),
    "watch_enabled": True,  # auto-detect + import on card/camera insert
    "mtp_enabled": True,  # also watch for the camera plugged in over USB/MTP
    "delete_originals_after_import": False,
    "notify_on_complete": True,
    "dng_compression": "lossless",  # lossless | uncompressed
    "embed_raw_in_dng": False,  # off matches Lightroom's ~50% size reduction
    "dnglab_path": "",  # filled in once resolved/downloaded
}


def get(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key)
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
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            merged[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            merged[row["key"]] = row["value"]
    return merged
