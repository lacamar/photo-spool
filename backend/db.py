"""SQLite schema and versioned migrations (tracked via PRAGMA user_version)."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from . import paths

# Each entry is the full set of statements that take the DB from version
# (index) to (index + 1). Add new migrations by appending a new list; never
# edit an already-shipped migration.
MIGRATIONS: Sequence[Sequence[str]] = (
    # --- v0 -> v1 ---
    (
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            device_label TEXT NOT NULL,
            source_root TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'blockdev' CHECK (kind IN ('blockdev', 'mtp', 'manual')),
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
            found_count INTEGER NOT NULL DEFAULT 0,
            imported_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            bytes_saved INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            ejectable_path TEXT NOT NULL DEFAULT '',
            ejected INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX idx_sessions_started ON sessions(started_at)",
        """
        CREATE TABLE session_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            source_filename TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('imported', 'duplicate', 'failed')),
            dest_path TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL
        )
        """,
        "CREATE INDEX idx_session_files_session ON session_files(session_id, sort_order)",
        """
        CREATE TABLE imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_hash TEXT NOT NULL UNIQUE,
            source_filename TEXT NOT NULL,
            source_bytes INTEGER NOT NULL,
            camera_model TEXT NOT NULL DEFAULT '',
            captured_at TEXT,
            dest_path TEXT NOT NULL,
            dest_bytes INTEGER NOT NULL,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            imported_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_imports_quickmatch ON imports(camera_model, source_filename, source_bytes)",
        "CREATE INDEX idx_imports_session ON imports(session_id)",
        """
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            text TEXT NOT NULL,
            session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
            kind TEXT NOT NULL DEFAULT 'other',
            read INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ),
    # --- v1 -> v2: manually pinned import folders, shown alongside live
    # devices in the source strip ---
    (
        """
        CREATE TABLE saved_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
    ),
    # --- v2 -> v3: "mark as already imported" becomes a quiet session
    # (sessions.mark_only) that AppController skips surfacing as a history
    # card or notification. And since clearing history deletes sessions
    # rows, imports.session_id needs to survive that -- switched from
    # NOT NULL / ON DELETE CASCADE to nullable / ON DELETE SET NULL so the
    # dedup ledger (and lifetime stats) outlive a cleared session. SQLite
    # can't ALTER a column's constraints in place, hence the rebuild.
    (
        "ALTER TABLE sessions ADD COLUMN mark_only INTEGER NOT NULL DEFAULT 0",
        """
        CREATE TABLE imports_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_hash TEXT NOT NULL UNIQUE,
            source_filename TEXT NOT NULL,
            source_bytes INTEGER NOT NULL,
            camera_model TEXT NOT NULL DEFAULT '',
            captured_at TEXT,
            dest_path TEXT NOT NULL,
            dest_bytes INTEGER NOT NULL,
            session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
            imported_at TEXT NOT NULL
        )
        """,
        "INSERT INTO imports_new SELECT * FROM imports",
        "DROP TABLE imports",
        "ALTER TABLE imports_new RENAME TO imports",
        "CREATE INDEX idx_imports_quickmatch ON imports(camera_model, source_filename, source_bytes)",
        "CREATE INDEX idx_imports_session ON imports(session_id)",
    ),
    # --- v3 -> v4: per-file exiftool metadata cache, keyed by path alone
    # (no size/mtime re-check) -- confirmed live that reading metadata for
    # an iPhone's ~700-file DCIM tree via exiftool took nearly 21 seconds,
    # on every single scan (picker open, source-card stats refresh, and
    # an actual import's checking phase), even though the same few
    # hundred files are unchanged scan to scan. Relies on the same
    # never-modify-the-source invariant this app already depends on
    # elsewhere (see import_worker.py/paths.py) -- once a file exists on
    # a card/phone's camera roll, this app never touches it, so its
    # metadata can never actually change out from under a cached entry.
    (
        """
        CREATE TABLE metadata_cache (
            path TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL,
            camera_model TEXT NOT NULL DEFAULT '',
            captured_at TEXT,
            camera_model_inferred INTEGER NOT NULL DEFAULT 0,
            cached_at TEXT NOT NULL
        )
        """,
    ),
    # --- v4 -> v5: in-app notification history removed ---
    (
        "DROP TABLE notifications",
    ),
)


def connect() -> sqlite3.Connection:
    paths.ensure_dirs()
    conn = sqlite3.connect(str(paths.db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Default (rollback-journal, synchronous=FULL) pays two-plus fsyncs per
    # commit -- and import_worker._record_file commits once per *file*
    # during the checking phase, including every quick-match duplicate hit
    # that did no hashing at all. On a re-scan of a mostly-already-imported
    # card that's hundreds of pure-fsync commits back to back, easily
    # dwarfing the actual dedup work and making "checking" look hung for
    # files that were never even read. WAL + synchronous=NORMAL (SQLite's
    # own recommended pairing) folds those into periodic checkpoint syncs
    # instead of one per commit; this is a single-user local desktop DB, so
    # the only durability cost is the (rare, OS-crash/power-loss-only,
    # re-scan-recoverable) chance of losing the last few ledger writes.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in enumerate(MIGRATIONS[current:], start=current + 1):
        with conn:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version}")
