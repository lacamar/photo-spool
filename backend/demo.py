"""Seed data for `--demo`, so the UI can be evaluated without a card or camera."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

# device_label, kind, minutes_ago, found, imported, duplicate, failed, bytes_saved
DEMO_SESSIONS = [
    ("SONY SD Card", "blockdev", 5, 42, 42, 0, 0, 42 * 40_000_000),
    ("ILCE-7RM3", "mtp", 60 * 26, 18, 11, 7, 0, 11 * 38_000_000),
    ("SONY SD Card", "blockdev", 60 * 24 * 3, 3, 0, 0, 3, 0),
]

DEMO_FILES = [
    ("DSC01075.ARW", "imported", "2026/2026-08/2026-08-02/2026.08.02_ILCE-7RM3_01075.dng", ""),
    ("DSC01076.ARW", "imported", "2026/2026-08/2026-08-02/2026.08.02_ILCE-7RM3_01076.dng", ""),
    ("DSC01077.ARW", "duplicate", "", ""),
]


def seed_if_empty(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
    if existing > 0:
        return

    now = datetime.now(timezone.utc)
    with conn:
        for (label, kind, minutes_ago, found, imported, duplicate, failed, bytes_saved) in DEMO_SESSIONS:
            started = now - timedelta(minutes=minutes_ago)
            finished = started + timedelta(minutes=3)
            status = "failed" if failed and not imported and not duplicate else "completed"
            error_message = "Card became unreadable partway through" if status == "failed" else ""
            cur = conn.execute(
                "INSERT INTO sessions (started_at, finished_at, device_label, source_root, kind, status, "
                "found_count, imported_count, duplicate_count, failed_count, bytes_saved, error_message, "
                "ejectable_path, ejected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    started.isoformat(), finished.isoformat(), label, "/run/media/demo/SONY", kind, status,
                    found, imported, duplicate, failed, bytes_saved, error_message,
                    "/run/media/demo/SONY" if kind != "mtp" else "", 0,
                ),
            )
            session_id = cur.lastrowid
            if imported or duplicate:
                for i, (fname, fstatus, dest, err) in enumerate(DEMO_FILES):
                    conn.execute(
                        "INSERT INTO session_files (session_id, source_filename, status, dest_path, "
                        "error_message, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, fname, fstatus, dest, err, i),
                    )
            if status == "completed":
                text = f"{label}: {imported} imported" + (f", {duplicate} already had copies" if duplicate else "")
                kind_notif = "import_complete"
            else:
                text = f"{label}: import failed -- {error_message}"
                kind_notif = "error"
            conn.execute(
                "INSERT INTO notifications (created_at, text, session_id, kind, read) VALUES (?, ?, ?, ?, ?)",
                (finished.isoformat(), text, session_id, kind_notif, 1 if minutes_ago > 60 else 0),
            )
