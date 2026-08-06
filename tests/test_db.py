from __future__ import annotations

import sqlite3

from backend import db
from tests.testutil import IsolatedTestCase


class FreshDatabaseTests(IsolatedTestCase):
    def _connect(self) -> sqlite3.Connection:
        conn = db.connect()
        self.addCleanup(conn.close)
        return conn

    def test_migrates_to_latest_version(self):
        conn = self._connect()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))

    def test_sessions_has_mark_only_column(self):
        conn = self._connect()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        self.assertIn("mark_only", cols)

    def test_imports_session_id_is_nullable(self):
        conn = self._connect()
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
        # column index 3 in PRAGMA table_info is "notnull"
        self.assertEqual(cols["session_id"][3], 0, "imports.session_id must be nullable")


class UpgradeFromV2Tests(IsolatedTestCase):
    """Simulates upgrading an existing install's DB (schema versions 1-2,
    predating mark_only and the nullable imports.session_id) to confirm
    real user data survives the migration -- this is the exact scenario
    that surfaced the CASCADE-delete bug clearing history used to have."""

    def _build_v2_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        for stmts in db.MIGRATIONS[:2]:
            for stmt in stmts:
                conn.execute(stmt)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        return conn

    def test_existing_data_survives_migration(self):
        conn = self._build_v2_db()
        cur = conn.execute(
            "INSERT INTO sessions (started_at, device_label, source_root, kind, status) "
            "VALUES ('t', 'SD', '/root', 'blockdev', 'completed')"
        )
        session_id = cur.lastrowid
        conn.execute(
            "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, "
            "captured_at, dest_path, dest_bytes, session_id, imported_at) "
            "VALUES ('h', 'a.dng', 100, 'X', 't', '/lib/a.dng', 90, ?, 't')",
            (session_id,),
        )
        conn.commit()

        db.migrate(conn)

        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        row = conn.execute("SELECT * FROM imports WHERE source_hash = 'h'").fetchone()
        self.assertEqual(row["dest_path"], "/lib/a.dng")
        self.assertEqual(row["session_id"], session_id)

    def test_clearing_a_session_after_migration_preserves_the_ledger(self):
        conn = self._build_v2_db()
        cur = conn.execute(
            "INSERT INTO sessions (started_at, device_label, source_root, kind, status) "
            "VALUES ('t', 'SD', '/root', 'blockdev', 'completed')"
        )
        session_id = cur.lastrowid
        conn.execute(
            "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, "
            "captured_at, dest_path, dest_bytes, session_id, imported_at) "
            "VALUES ('h', 'a.dng', 100, 'X', 't', '/lib/a.dng', 90, ?, 't')",
            (session_id,),
        )
        conn.commit()
        db.migrate(conn)

        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()

        row = conn.execute("SELECT session_id, dest_path FROM imports WHERE source_hash = 'h'").fetchone()
        self.assertIsNotNone(row, "the dedup ledger row must survive clearing its session")
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["dest_path"], "/lib/a.dng")
