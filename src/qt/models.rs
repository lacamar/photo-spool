//! Session history and source strip models. Rows are JSON maps keyed by role name.
use std::pin::Pin;

use chrono::{DateTime, Local};
use cxx_qt::CxxQtType;
use cxx_qt_lib::{QByteArray, QHash, QHashPair_i32_QByteArray, QModelIndex, QVariant, QVector};
use rusqlite::{Connection, Row};
use serde_json::{Map, Value, json};

use super::bridge::qobject::{SessionListModel, SourceListModel};
use super::convert::to_qvariant;

pub type Entry = Map<String, Value>;

#[derive(Default)]
pub struct SessionRows {
    entries: Vec<Entry>,
}

#[derive(Default)]
pub struct SourceRows {
    entries: Vec<Entry>,
}

const USER_ROLE: i32 = 0x0100;

const SESSION_ROLES: &[&str] = &[
    "sessionId",
    "startedAt",
    "finishedAt",
    "deviceLabel",
    "sourceRoot",
    "kind",
    "kindLabel",
    "status",
    "foundCount",
    "importedCount",
    "duplicateCount",
    "failedCount",
    "bytesSaved",
    "errorMessage",
    "startedDay",
    "progressPhase",
    "progressDone",
    "progressTotal",
    "progressFile",
];

// "rootPath", never "root": delegates expose roles as bare identifiers, shadowing `id: root`
const SOURCE_ROLES: &[&str] = &[
    "sourceKey",
    "label",
    "kind",
    "mounted",
    "rootPath",
    "removable",
    "statsLoaded",
    "fileCount",
    "newCount",
    "contentBytes",
    "cameraModel",
    "capacityBytes",
    "usedBytes",
    "freeBytes",
];
const PROGRESS_KEYS: [&str; 4] = [
    "progressPhase",
    "progressDone",
    "progressTotal",
    "progressFile",
];
const STATS_KEYS: [&str; 8] = [
    "statsLoaded",
    "fileCount",
    "newCount",
    "contentBytes",
    "cameraModel",
    "capacityBytes",
    "usedBytes",
    "freeBytes",
];

fn stats_defaults() -> Entry {
    let Value::Object(m) = json!({
        "statsLoaded": false, "fileCount": 0, "newCount": 0, "contentBytes": 0, "cameraModel": "",
        "capacityBytes": 0, "usedBytes": 0, "freeBytes": 0,
    }) else {
        unreachable!()
    };
    m
}

macro_rules! list_model {
    ($ty:ty, $roles:expr) => {
        impl $ty {
            pub fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
                let name = usize::try_from(role - USER_ROLE - 1)
                    .ok()
                    .and_then(|i| $roles.get(i));
                match (self.entries.get(index.row() as usize), name) {
                    (Some(e), Some(n)) => e.get(*n).map(to_qvariant).unwrap_or_default(),
                    _ => QVariant::default(),
                }
            }

            pub fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
                let mut h = QHash::<QHashPair_i32_QByteArray>::default();
                for (i, n) in $roles.iter().enumerate() {
                    h.insert(USER_ROLE + 1 + i as i32, QByteArray::from(*n));
                }
                h
            }

            pub fn row_count(&self, _parent: &QModelIndex) -> i32 {
                self.entries.len() as i32
            }

            fn position(&self, key: &str, value: &Value) -> Option<usize> {
                self.entries.iter().position(|e| e.get(key) == Some(value))
            }

            fn changed(mut self: Pin<&mut Self>, i: usize) {
                let idx = self.index(i as i32, 0, &QModelIndex::default());
                self.as_mut().data_changed(&idx, &idx, &QVector::default());
            }

            fn insert_at(mut self: Pin<&mut Self>, i: usize, entry: Entry) {
                self.as_mut()
                    .begin_insert_rows(&QModelIndex::default(), i as i32, i as i32);
                self.as_mut().rust_mut().entries.insert(i, entry);
                self.as_mut().end_insert_rows();
            }

            fn remove_at(mut self: Pin<&mut Self>, i: usize) {
                self.as_mut()
                    .begin_remove_rows(&QModelIndex::default(), i as i32, i as i32);
                self.as_mut().rust_mut().entries.remove(i);
                self.as_mut().end_remove_rows();
            }
        }
    };
}

list_model!(SessionListModel, SESSION_ROLES);
list_model!(SourceListModel, SOURCE_ROLES);

fn kind_label(kind: &str) -> String {
    match kind {
        "blockdev" => "SD card / mass storage",
        "mtp" => "Camera / phone (USB)",
        "manual" => "Manual import",
        other => other,
    }
    .into()
}

fn local_day(iso: &str) -> String {
    DateTime::parse_from_rfc3339(iso)
        .map(|d| d.with_timezone(&Local).date_naive().to_string())
        .unwrap_or_default()
}

fn session_entry(r: &Row) -> rusqlite::Result<Entry> {
    let started: String = r.get("started_at")?;
    let kind: String = r.get("kind")?;
    let Value::Object(m) = json!({
        "sessionId": r.get::<_, i64>("id")?,
        "startedAt": started,
        "finishedAt": r.get::<_, Option<String>>("finished_at")?.unwrap_or_default(),
        "deviceLabel": r.get::<_, String>("device_label")?,
        "sourceRoot": r.get::<_, String>("source_root")?,
        "kindLabel": kind_label(&kind),
        "kind": kind,
        "status": r.get::<_, String>("status")?,
        "foundCount": r.get::<_, i64>("found_count")?,
        "importedCount": r.get::<_, i64>("imported_count")?,
        "duplicateCount": r.get::<_, i64>("duplicate_count")?,
        "failedCount": r.get::<_, i64>("failed_count")?,
        "bytesSaved": r.get::<_, i64>("bytes_saved")?,
        "errorMessage": r.get::<_, String>("error_message")?,
        "startedDay": local_day(&started),
        "progressPhase": "",
        "progressDone": 0,
        "progressTotal": 0,
        "progressFile": "",
    }) else {
        unreachable!()
    };
    Ok(m)
}

impl SessionListModel {
    /// mark_only sessions are quiet and never shown.
    pub fn load(mut self: Pin<&mut Self>, conn: &Connection) {
        let entries = conn
            .prepare("SELECT * FROM sessions WHERE mark_only = 0 ORDER BY id DESC")
            .and_then(|mut s| {
                s.query_map([], session_entry)?
                    .collect::<rusqlite::Result<Vec<_>>>()
            })
            .unwrap_or_else(|e| {
                log::error!("loading sessions: {e}");
                vec![]
            });
        self.as_mut().begin_reset_model();
        self.as_mut().rust_mut().entries = entries;
        self.as_mut().end_reset_model();
    }

    /// Refreshes stored fields from the DB, keeping in-memory progress; new sessions go on top.
    pub fn upsert(mut self: Pin<&mut Self>, session_id: i64, conn: &Connection) {
        let Ok(mut entry) = conn.query_row(
            "SELECT * FROM sessions WHERE id = ?",
            [session_id],
            session_entry,
        ) else {
            return;
        };
        match self.position("sessionId", &json!(session_id)) {
            Some(i) => {
                for k in PROGRESS_KEYS {
                    entry.insert(k.into(), self.entries[i][k].clone());
                }
                self.as_mut().rust_mut().entries[i] = entry;
                self.changed(i);
            }
            None => self.insert_at(0, entry),
        }
    }

    pub fn set_progress(
        mut self: Pin<&mut Self>,
        session_id: i64,
        phase: &str,
        done: i64,
        total: i64,
        file: &str,
    ) {
        let Some(i) = self.position("sessionId", &json!(session_id)) else {
            return;
        };
        let e = &mut self.as_mut().rust_mut().entries[i];
        e.insert("progressPhase".into(), json!(phase));
        e.insert("progressDone".into(), json!(done));
        e.insert("progressTotal".into(), json!(total));
        e.insert("progressFile".into(), json!(file));
        self.changed(i);
    }

    pub fn remove(self: Pin<&mut Self>, session_id: i64) {
        if let Some(i) = self.position("sessionId", &json!(session_id)) {
            self.remove_at(i);
        }
    }

    pub fn remove_all_except(mut self: Pin<&mut Self>, keep: i64) {
        for i in (0..self.entries.len()).rev() {
            if self.entries[i]["sessionId"] != json!(keep) {
                self.as_mut().remove_at(i);
            }
        }
    }
}

impl SourceListModel {
    pub fn entry_for(&self, key: &str) -> Option<Entry> {
        self.position("sourceKey", &json!(key))
            .map(|i| self.entries[i].clone())
    }

    pub fn all_entries(&self) -> Vec<Entry> {
        self.entries.clone()
    }

    /// Keeps loaded stats across a re-upsert so cards don't flicker each poll.
    pub fn upsert(
        mut self: Pin<&mut Self>,
        key: &str,
        label: &str,
        kind: &str,
        mounted: bool,
        root: &str,
        removable: bool,
    ) {
        let mut entry = Map::new();
        for (k, v) in [
            ("sourceKey", json!(key)),
            ("label", json!(label)),
            ("kind", json!(kind)),
            ("mounted", json!(mounted)),
            ("rootPath", json!(root)),
            ("removable", json!(removable)),
        ] {
            entry.insert(k.into(), v);
        }
        entry.extend(stats_defaults());
        match self.position("sourceKey", &json!(key)) {
            Some(i) => {
                for k in STATS_KEYS {
                    entry.insert(k.into(), self.entries[i][k].clone());
                }
                self.as_mut().rust_mut().entries[i] = entry;
                self.changed(i);
            }
            None => {
                let n = self.entries.len();
                self.insert_at(n, entry);
            }
        }
    }

    /// Stats belong to a specific mount: reset them when it changes.
    pub fn set_mounted(mut self: Pin<&mut Self>, key: &str, mounted: bool, root: &str) {
        let Some(i) = self.position("sourceKey", &json!(key)) else {
            return;
        };
        let e = &mut self.as_mut().rust_mut().entries[i];
        e.insert("mounted".into(), json!(mounted));
        e.insert("rootPath".into(), json!(root));
        e.extend(stats_defaults());
        self.changed(i);
    }

    pub fn set_stats(mut self: Pin<&mut Self>, key: &str, stats: Value) {
        let Some(i) = self.position("sourceKey", &json!(key)) else {
            return;
        };
        let e = &mut self.as_mut().rust_mut().entries[i];
        if let Value::Object(s) = stats {
            e.extend(s);
        }
        e.insert("statsLoaded".into(), json!(true));
        self.changed(i);
    }

    pub fn remove(self: Pin<&mut Self>, key: &str) {
        if let Some(i) = self.position("sourceKey", &json!(key)) {
            self.remove_at(i);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_day_converts_utc_to_local_date() {
        let iso = "2026-08-02T12:00:00.000001+00:00";
        let expect = DateTime::parse_from_rfc3339(iso)
            .unwrap()
            .with_timezone(&Local)
            .date_naive()
            .to_string();
        assert_eq!(local_day(iso), expect);
        assert_eq!(local_day("garbage"), "");
    }
}
