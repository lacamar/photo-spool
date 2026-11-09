//! Background scans for the source picker (latest request wins) and source-card stats (FIFO, deduped by key).
use std::path::Path;
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;

use rusqlite::Connection;
use serde_json::{Value, json};

use crate::{db, scanner, util};

struct Queue<T> {
    state: Mutex<(T, bool)>,
    cond: Condvar,
}

/// One thread, one DB connection, fed by a shared queue `T`.
pub struct Worker<T> {
    queue: Arc<Queue<T>>,
    thread: Option<JoinHandle<()>>,
}

impl<T: Default + Send + 'static> Worker<T> {
    fn start(
        mut take: impl FnMut(&mut T) -> Option<(String, String)> + Send + 'static,
        mut handle: impl FnMut(&Connection, String, String) + Send + 'static,
    ) -> Self {
        let queue = Arc::new(Queue {
            state: Mutex::new((T::default(), false)),
            cond: Condvar::new(),
        });
        let q = queue.clone();
        let thread = std::thread::spawn(move || {
            let conn = match db::connect() {
                Ok(c) => c,
                Err(e) => return log::error!("worker: cannot open DB: {e}"),
            };
            loop {
                let job = {
                    let mut s = q.state.lock().unwrap();
                    loop {
                        if s.1 {
                            return;
                        }
                        if let Some(job) = take(&mut s.0) {
                            break job;
                        }
                        s = q.cond.wait(s).unwrap();
                    }
                };
                handle(&conn, job.0, job.1);
            }
        });
        Self {
            queue,
            thread: Some(thread),
        }
    }

    fn push(&self, f: impl FnOnce(&mut T)) {
        f(&mut self.queue.state.lock().unwrap().0);
        self.queue.cond.notify_one();
    }

    pub fn stop(&mut self) {
        self.queue.state.lock().unwrap().1 = true;
        self.queue.cond.notify_one();
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

pub type PreviewWorker = Worker<Option<(String, String)>>;
pub type StatsWorker = Worker<Vec<(String, String)>>;

pub fn start_preview(emit: impl Fn(String, Value) + Send + 'static) -> PreviewWorker {
    Worker::start(Option::take, move |conn, key, root| {
        emit(key, preview_scan(conn, &root))
    })
}

impl PreviewWorker {
    /// Supersedes any pending scan: only the latest-opened panel matters.
    pub fn request(&self, key: &str, root: &str) {
        self.push(|slot| *slot = Some((key.into(), root.into())));
    }
}

pub fn start_stats(emit: impl Fn(String, Value) + Send + 'static) -> StatsWorker {
    Worker::start(
        |q: &mut Vec<(String, String)>| (!q.is_empty()).then(|| q.remove(0)),
        move |conn, key, root| emit(key, compute_stats(conn, &root)),
    )
}

impl StatsWorker {
    /// Re-requesting a queued key keeps its place and updates its root.
    pub fn request(&self, key: &str, root: &str) {
        if root.is_empty() {
            return;
        }
        self.push(|q| match q.iter_mut().find(|(k, _)| k == key) {
            Some(e) => e.1 = root.into(),
            None => q.push((key.into(), root.into())),
        });
    }
}

/// Metadata plus the cheap (model, filename, size) pre-check only; no hashing.
pub fn preview_scan(conn: &Connection, root: &str) -> Value {
    let files = scanner::find_importable_files(Path::new(root));
    let metadata = scanner::read_metadata(&files, Some(conn));
    let mut items: Vec<Value> = files
        .iter()
        .filter_map(|f| {
            let c = metadata.get(f)?;
            let name = util::file_name(f);
            let already =
                scanner::quick_duplicate_match(conn, &c.camera_model, &name, c.size_bytes)
                    .ok()?
                    .is_some();
            Some(json!({
                "filename": name,
                "path": util::lossy(f),
                "sizeBytes": c.size_bytes,
                "capturedAt": c.captured_at.clone().unwrap_or_default(),
                "alreadyImported": already,
            }))
        })
        .collect();
    items.sort_by(|a, b| a["filename"].as_str().cmp(&b["filename"].as_str()));
    Value::Array(items)
}

fn disk_usage(root: &str) -> Option<(u64, u64, u64)> {
    let c = std::ffi::CString::new(root).ok()?;
    let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: c is a valid C string and s a properly sized out-param
    if unsafe { libc::statvfs(c.as_ptr(), &mut s) } != 0 {
        return None;
    }
    let f = s.f_frsize as u64;
    Some((
        s.f_blocks as u64 * f,
        (s.f_blocks - s.f_bfree) as u64 * f,
        s.f_bavail as u64 * f,
    ))
}

pub fn compute_stats(conn: &Connection, root: &str) -> Value {
    // gvfs mounts don't always support statvfs: stats-less card is fine
    let (capacity, used, free) = disk_usage(root).unwrap_or_default();
    let (mut file_count, mut new_count, mut content_bytes) = (0, 0, 0i64);
    let mut models: Vec<(String, usize)> = Vec::new();
    if Path::new(root).is_dir() {
        let files = scanner::find_importable_files(Path::new(root));
        let metadata = scanner::read_metadata(&files, Some(conn));
        file_count = files.len();
        for f in &files {
            let Some(c) = metadata.get(f) else { continue };
            content_bytes += c.size_bytes;
            if !c.camera_model.is_empty() {
                match models.iter_mut().find(|(m, _)| *m == c.camera_model) {
                    Some(e) => e.1 += 1,
                    None => models.push((c.camera_model.clone(), 1)),
                }
            }
            let known = scanner::quick_duplicate_match(
                conn,
                &c.camera_model,
                &util::file_name(f),
                c.size_bytes,
            );
            if matches!(known, Ok(None)) {
                new_count += 1;
            }
        }
    }
    // Counter.most_common: ties go to the first seen
    let top = models
        .iter()
        .fold(None::<&(String, usize)>, |best, e| match best {
            Some(b) if b.1 >= e.1 => Some(b),
            _ => Some(e),
        });
    json!({
        "fileCount": file_count,
        "newCount": new_count,
        "contentBytes": content_bytes,
        "cameraModel": top.map_or("", |t| t.0.as_str()),
        "capacityBytes": capacity,
        "usedBytes": used,
        "freeBytes": free,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::Isolated;

    #[test]
    fn stats_and_preview_over_cached_metadata() {
        let iso = Isolated::new();
        let conn = db::connect().unwrap();
        let card = iso.root.join("card");
        std::fs::create_dir_all(&card).unwrap();
        for (name, model) in [("b.arw", "A"), ("a.arw", "B"), ("c.arw", "B")] {
            let p = card.join(name);
            std::fs::write(&p, b"xx").unwrap();
            conn.execute(
                "INSERT INTO metadata_cache (path, size_bytes, camera_model, captured_at, cached_at) VALUES (?, 2, ?, NULL, 't')",
                rusqlite::params![util::lossy(&p), model],
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, dest_path, dest_bytes, imported_at) \
             VALUES ('h', 'a.arw', 2, 'B', '/x', 1, 't')",
            [],
        )
        .unwrap();
        let root = util::lossy(&card);
        let stats = compute_stats(&conn, &root);
        assert_eq!(stats["fileCount"], 3);
        assert_eq!(stats["newCount"], 2);
        assert_eq!(stats["contentBytes"], 6);
        assert_eq!(stats["cameraModel"], "B");
        assert!(stats["capacityBytes"].as_u64().unwrap() > 0);
        let items = preview_scan(&conn, &root);
        let names: Vec<&str> = items
            .as_array()
            .unwrap()
            .iter()
            .map(|i| i["filename"].as_str().unwrap())
            .collect();
        assert_eq!(names, ["a.arw", "b.arw", "c.arw"]);
        assert_eq!(items[0]["alreadyImported"], true);
        assert_eq!(items[1]["capturedAt"], "");
    }
}
