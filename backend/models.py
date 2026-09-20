"""Plain dataclasses mirroring DB rows. No behaviour, just shape."""
from __future__ import annotations

from dataclasses import dataclass

SESSION_KINDS = ("blockdev", "mtp", "manual")
SESSION_STATUSES = ("running", "completed", "failed", "cancelled")
SESSION_FILE_STATUSES = ("imported", "duplicate", "failed")

SESSION_KIND_LABELS = {
    "blockdev": "SD card / mass storage",
    "mtp": "Camera / phone (USB)",
    "manual": "Manual import",
}


@dataclass
class Session:
    id: int
    started_at: str
    finished_at: str | None
    device_label: str
    source_root: str
    kind: str
    status: str
    found_count: int
    imported_count: int
    duplicate_count: int
    failed_count: int
    bytes_saved: int
    error_message: str
    ejectable_path: str
    ejected: bool


@dataclass
class SessionFile:
    id: int
    session_id: int
    source_filename: str
    status: str
    dest_path: str
    error_message: str
    sort_order: int
