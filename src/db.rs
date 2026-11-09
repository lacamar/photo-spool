use chrono::{Duration, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Value, json};

use crate::paths;

// Append-only: never edit an already-shipped migration.
const MIGRATIONS: &[&[&str]] = &[
    &[
        "CREATE TABLE sessions (
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
        )",
        "CREATE INDEX idx_sessions_started ON sessions(started_at)",
        "CREATE TABLE session_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            source_filename TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('imported', 'duplicate', 'failed')),
            dest_path TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL
        )",
        "CREATE INDEX idx_session_files_session ON session_files(session_id, sort_order)",
        "CREATE TABLE imports (
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
        )",
        "CREATE INDEX idx_imports_quickmatch ON imports(camera_model, source_filename, source_bytes)",
        "CREATE INDEX idx_imports_session ON imports(session_id)",
        "CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            text TEXT NOT NULL,
            session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
            kind TEXT NOT NULL DEFAULT 'other',
            read INTEGER NOT NULL DEFAULT 0
        )",
        "CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )",
    ],
    &["CREATE TABLE saved_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )"],
    // imports.session_id becomes nullable/SET NULL so the dedup ledger outlives cleared history
    &[
        "ALTER TABLE sessions ADD COLUMN mark_only INTEGER NOT NULL DEFAULT 0",
        "CREATE TABLE imports_new (
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
        )",
        "INSERT INTO imports_new SELECT * FROM imports",
        "DROP TABLE imports",
        "ALTER TABLE imports_new RENAME TO imports",
        "CREATE INDEX idx_imports_quickmatch ON imports(camera_model, source_filename, source_bytes)",
        "CREATE INDEX idx_imports_session ON imports(session_id)",
    ],
    &["CREATE TABLE metadata_cache (
            path TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL,
            camera_model TEXT NOT NULL DEFAULT '',
            captured_at TEXT,
            camera_model_inferred INTEGER NOT NULL DEFAULT 0,
            cached_at TEXT NOT NULL
        )"],
    &["DROP TABLE notifications"],
];

pub fn now_utc() -> String {
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}

pub fn connect() -> rusqlite::Result<Connection> {
    paths::ensure_dirs().map_err(|e| rusqlite::Error::ToSqlConversionFailure(Box::new(e)))?;
    let mut conn = Connection::open(paths::db_path())?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    // WAL + NORMAL: one fsync per checkpoint instead of per per-file commit
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    migrate(&mut conn)?;
    Ok(conn)
}

pub fn migrate(conn: &mut Connection) -> rusqlite::Result<()> {
    let current: i64 = conn.pragma_query_value(None, "user_version", |r| r.get(0))?;
    for (i, statements) in MIGRATIONS.iter().enumerate().skip(current as usize) {
        let tx = conn.transaction()?;
        for stmt in *statements {
            tx.execute(stmt, [])?;
        }
        tx.pragma_update(None, "user_version", i as i64 + 1)?;
        tx.commit()?;
    }
    Ok(())
}

#[cfg(test)]
fn schema_version() -> usize {
    MIGRATIONS.len()
}

// --- settings: JSON-encoded values in the `settings` table ---

fn defaults() -> Map<String, Value> {
    let Value::Object(m) = json!({
        "theme_mode": "system",
        "watch_enabled": true,
        "mtp_enabled": true,
        "delete_originals_after_import": false,
        "notify_on_complete": true,
        "dng_compression": "lossless",
        "embed_raw_in_dng": false,
        "preview_size": "medium",
    }) else {
        unreachable!()
    };
    m
}

// library_root is resolved per call: XDG_PICTURES_DIR must never be baked in early
fn default_for(key: &str) -> Value {
    if key == "library_root" {
        return Value::String(paths::default_library_root().to_string_lossy().into_owned());
    }
    defaults().remove(key).unwrap_or(Value::Null)
}

fn decode(raw: String) -> Value {
    serde_json::from_str(&raw).unwrap_or(Value::String(raw))
}

pub fn get_setting(conn: &Connection, key: &str) -> Value {
    conn.query_row("SELECT value FROM settings WHERE key = ?", [key], |r| {
        r.get::<_, String>(0)
    })
    .optional()
    .ok()
    .flatten()
    .map_or_else(|| default_for(key), decode)
}

pub fn set_setting(conn: &Connection, key: &str, value: &Value) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) \
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![key, value.to_string()],
    )?;
    Ok(())
}

pub fn all_settings(conn: &Connection) -> rusqlite::Result<Map<String, Value>> {
    let mut merged = defaults();
    merged.insert("library_root".into(), default_for("library_root"));
    let mut stmt = conn.prepare("SELECT key, value FROM settings")?;
    let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
    for row in rows {
        let (k, v) = row?;
        merged.insert(k, decode(v));
    }
    Ok(merged)
}

pub fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

pub fn str_or<'a>(v: Option<&'a Value>, fallback: &'a str) -> &'a str {
    v.and_then(Value::as_str).unwrap_or(fallback)
}

// --- --demo seed data ---

pub fn seed_demo_if_empty(conn: &mut Connection) -> rusqlite::Result<()> {
    let existing: i64 = conn.query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))?;
    if existing > 0 {
        return Ok(());
    }
    type Demo = (&'static str, &'static str, i64, i64, i64, i64, i64, i64);
    const SESSIONS: [Demo; 3] = [
        ("SONY SD Card", "blockdev", 5, 42, 42, 0, 0, 42 * 40_000_000),
        ("ILCE-7RM3", "mtp", 60 * 26, 18, 11, 7, 0, 11 * 38_000_000),
        ("SONY SD Card", "blockdev", 60 * 24 * 3, 3, 0, 0, 3, 0),
    ];
    const FILES: [(&str, &str, &str); 3] = [
        (
            "DSC01075.ARW",
            "imported",
            "2026/2026-08/2026-08-02/2026.08.02_ILCE-7RM3_01075.dng",
        ),
        (
            "DSC01076.ARW",
            "imported",
            "2026/2026-08/2026-08-02/2026.08.02_ILCE-7RM3_01076.dng",
        ),
        ("DSC01077.ARW", "duplicate", ""),
    ];
    let now = Utc::now();
    let tx = conn.transaction()?;
    for (label, kind, minutes_ago, found, imported, duplicate, failed, bytes_saved) in SESSIONS {
        let started = now - Duration::minutes(minutes_ago);
        let finished = started + Duration::minutes(3);
        let failed_status = failed > 0 && imported == 0 && duplicate == 0;
        let status = if failed_status { "failed" } else { "completed" };
        let error = if failed_status {
            "Card became unreadable partway through"
        } else {
            ""
        };
        let ejectable = if kind != "mtp" {
            "/run/media/demo/SONY"
        } else {
            ""
        };
        tx.execute(
            "INSERT INTO sessions (started_at, finished_at, device_label, source_root, kind, status, \
             found_count, imported_count, duplicate_count, failed_count, bytes_saved, error_message, \
             ejectable_path, ejected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            params![
                started.to_rfc3339(),
                finished.to_rfc3339(),
                label,
                "/run/media/demo/SONY",
                kind,
                status,
                found,
                imported,
                duplicate,
                failed,
                bytes_saved,
                error,
                ejectable
            ],
        )?;
        let session_id = tx.last_insert_rowid();
        if imported > 0 || duplicate > 0 {
            for (i, (name, fstatus, dest)) in FILES.iter().enumerate() {
                tx.execute(
                    "INSERT INTO session_files (session_id, source_filename, status, dest_path, \
                     error_message, sort_order) VALUES (?, ?, ?, ?, '', ?)",
                    params![session_id, name, fstatus, dest, i as i64],
                )?;
            }
        }
    }
    tx.commit()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::Isolated;

    fn tables(conn: &Connection) -> Vec<String> {
        let mut stmt = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
            .unwrap();
        stmt.query_map([], |r| r.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect()
    }

    #[test]
    fn fresh_db_reaches_latest_schema() {
        let _iso = Isolated::new();
        let conn = connect().unwrap();
        let v: i64 = conn
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .unwrap();
        assert_eq!(v as usize, schema_version());
        assert_eq!(
            tables(&conn),
            [
                "imports",
                "metadata_cache",
                "saved_folders",
                "session_files",
                "sessions",
                "settings"
            ]
        );
        let mode: String = conn
            .pragma_query_value(None, "journal_mode", |r| r.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
    }

    #[test]
    fn connect_is_idempotent() {
        let _iso = Isolated::new();
        connect().unwrap();
        connect().unwrap();
    }

    #[test]
    fn v2_to_v3_keeps_imports_and_nulls_session_on_delete() {
        let _iso = Isolated::new();
        paths::ensure_dirs().unwrap();
        let mut conn = Connection::open(paths::db_path()).unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        for (i, stmts) in MIGRATIONS[..2].iter().enumerate() {
            for s in *stmts {
                conn.execute(s, []).unwrap();
            }
            conn.pragma_update(None, "user_version", i as i64 + 1)
                .unwrap();
        }
        conn.execute(
            "INSERT INTO sessions (started_at, device_label, source_root) VALUES ('t', 'd', '/r')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO imports (source_hash, source_filename, source_bytes, dest_path, dest_bytes, \
             session_id, imported_at) VALUES ('h', 'f', 1, '/d', 1, 1, 't')",
            [],
        )
        .unwrap();
        migrate(&mut conn).unwrap();
        conn.execute("DELETE FROM sessions", []).unwrap();
        let sid: Option<i64> = conn
            .query_row(
                "SELECT session_id FROM imports WHERE source_hash='h'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(sid, None);
    }

    #[test]
    fn settings_defaults_and_roundtrip() {
        let iso = Isolated::new();
        let conn = connect().unwrap();
        assert_eq!(get_setting(&conn, "theme_mode"), json!("system"));
        assert_eq!(get_setting(&conn, "watch_enabled"), json!(true));
        assert_eq!(get_setting(&conn, "nope"), Value::Null);
        assert_eq!(
            get_setting(&conn, "library_root"),
            json!(iso.root.join("pictures").to_string_lossy())
        );
        set_setting(&conn, "theme_mode", &json!("dark")).unwrap();
        set_setting(&conn, "watch_enabled", &json!(false)).unwrap();
        assert_eq!(get_setting(&conn, "theme_mode"), json!("dark"));
        let all = all_settings(&conn).unwrap();
        assert_eq!(all["watch_enabled"], json!(false));
        assert_eq!(all["preview_size"], json!("medium"));
    }

    #[test]
    fn non_json_setting_value_reads_back_as_string() {
        let _iso = Isolated::new();
        let conn = connect().unwrap();
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('x', 'not json')",
            [],
        )
        .unwrap();
        assert_eq!(get_setting(&conn, "x"), json!("not json"));
    }

    #[test]
    fn demo_seed_only_when_empty() {
        let _iso = Isolated::new();
        let mut conn = connect().unwrap();
        seed_demo_if_empty(&mut conn).unwrap();
        seed_demo_if_empty(&mut conn).unwrap();
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 3);
    }
}
