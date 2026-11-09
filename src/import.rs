//! Import pipeline: scan -> dedup -> convert -> place -> record, one request at a time on its own thread.
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use rusqlite::{Connection, ErrorCode, params};

use crate::scanner::{self, Candidate};
use crate::util::{self, file_name, lossy};
use crate::{converter, db, paths};

pub const PHASE_SCANNING: &str = "scanning";
pub const PHASE_CHECKING: &str = "checking";
pub const PHASE_CONVERTING: &str = "converting";
pub const PHASE_PLACING: &str = "placing";

#[derive(Debug, Clone, Default)]
pub struct ImportRequest {
    pub source_root: String,
    pub device_label: String,
    /// 'blockdev' | 'mtp' | 'manual'
    pub kind: String,
    /// None imports everything; otherwise only these original filenames are touched.
    pub selected_filenames: Option<HashSet<String>>,
    /// Record the dedup ledger only; never runs dnglab or writes to the library.
    pub mark_only: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    Started(i64),
    Progress {
        session_id: i64,
        phase: &'static str,
        done: i64,
        total: i64,
        file: String,
    },
    Finished(i64),
    DnglabUnavailable(i64),
}

pub fn find_dnglab() -> Option<PathBuf> {
    let path = util::which("dnglab")?;
    util::run_capture(Command::new(&path).arg("--version"), Duration::from_secs(5))
        .filter(|o| o.status.success())
        .map(|_| path)
}

#[derive(Default)]
struct PauseState {
    paused: bool,
    stopping: bool,
}

/// Pausing takes effect at safe boundaries only (between files, before a conversion batch,
/// before the next session) -- never mid-hash or mid-dnglab.
#[derive(Default)]
struct Pause {
    state: Mutex<PauseState>,
    cond: Condvar,
}

impl Pause {
    fn wait(&self) {
        let mut s = self.state.lock().unwrap();
        while s.paused && !s.stopping {
            s = self.cond.wait(s).unwrap();
        }
    }
}

pub struct Pipeline {
    emit: Box<dyn Fn(Event) + Send>,
    find_dnglab: Box<dyn Fn() -> Option<PathBuf> + Send>,
    pause: Arc<Pause>,
}

enum Msg {
    Run(ImportRequest),
    Stop,
}

pub struct ImportWorker {
    tx: Sender<Msg>,
    pause: Arc<Pause>,
    thread: Option<JoinHandle<()>>,
}

impl ImportWorker {
    pub fn start(emit: impl Fn(Event) + Send + 'static) -> Self {
        let pause = Arc::new(Pause::default());
        let (tx, rx) = mpsc::channel();
        let pipeline = Pipeline {
            emit: Box::new(emit),
            find_dnglab: Box::new(find_dnglab),
            pause: pause.clone(),
        };
        let thread = std::thread::spawn(move || pipeline.run(rx));
        Self {
            tx,
            pause,
            thread: Some(thread),
        }
    }

    pub fn submit(&self, request: ImportRequest) {
        let _ = self.tx.send(Msg::Run(request));
    }

    pub fn set_paused(&self, paused: bool) {
        self.pause.state.lock().unwrap().paused = paused;
        if !paused {
            self.pause.cond.notify_all();
        }
    }

    /// Also releases a paused import so shutdown never hangs on it.
    pub fn stop(&mut self) {
        {
            let mut s = self.pause.state.lock().unwrap();
            s.stopping = true;
            s.paused = false;
        }
        self.pause.cond.notify_all();
        let _ = self.tx.send(Msg::Stop);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

type Staged = (String, Candidate, i64, Option<PathBuf>);

impl Pipeline {
    #[cfg(test)]
    pub fn for_test(emit: impl Fn(Event) + Send + 'static, dnglab: Option<PathBuf>) -> Self {
        Self {
            emit: Box::new(emit),
            find_dnglab: Box::new(move || dnglab.clone()),
            pause: Arc::default(),
        }
    }

    fn run(self, rx: Receiver<Msg>) {
        let conn = match db::connect() {
            Ok(c) => c,
            Err(e) => return log::error!("import worker: cannot open DB: {e}"),
        };
        while let Ok(Msg::Run(request)) = rx.recv() {
            self.pause.wait();
            if let Err(e) = self.run_session(&conn, &request) {
                log::error!("Import session failed: {e}");
            }
        }
    }

    fn progress(
        &self,
        session_id: i64,
        phase: &'static str,
        done: usize,
        total: usize,
        file: impl Into<String>,
    ) {
        (self.emit)(Event::Progress {
            session_id,
            phase,
            done: done as i64,
            total: total as i64,
            file: file.into(),
        });
    }

    pub fn run_session(&self, conn: &Connection, request: &ImportRequest) -> anyhow::Result<()> {
        conn.execute(
            "INSERT INTO sessions (started_at, device_label, source_root, kind, status, mark_only) \
             VALUES (?, ?, ?, ?, 'running', ?)",
            params![
                db::now_utc(),
                request.device_label,
                request.source_root,
                request.kind,
                request.mark_only
            ],
        )?;
        let session_id = conn.last_insert_rowid();
        (self.emit)(Event::Started(session_id));

        let settings = db::all_settings(conn)?;
        let library_root = PathBuf::from(db::str_or(settings.get("library_root"), ""));
        let root = request.source_root.as_str();

        let mut files = scanner::find_importable_files(Path::new(root));
        conn.execute(
            "UPDATE sessions SET found_count = ? WHERE id = ?",
            params![files.len() as i64, session_id],
        )?;
        self.progress(session_id, PHASE_SCANNING, files.len(), files.len(), "");
        if files.is_empty() {
            return self.finish(conn, session_id, "completed", "", root);
        }

        // metadata (and model inference) over the whole card before narrowing to a selection,
        // so a partial selection of model-less videos still borrows the card's model
        let metadata = scanner::read_metadata(&files, Some(conn));

        if let Some(selected) = &request.selected_filenames {
            files.retain(|f| selected.contains(&file_name(f)));
            if files.is_empty() {
                return self.finish(conn, session_id, "completed", "", root);
            }
        }

        let mut dnglab = None;
        if !request.mark_only {
            dnglab = (self.find_dnglab)();
            if dnglab.is_none() {
                self.finish(
                    conn,
                    session_id,
                    "failed",
                    "The DNG converter (dnglab) isn't installed -- install the dnglab package, then retry from Settings.",
                    "",
                )?;
                (self.emit)(Event::DnglabUnavailable(session_id));
                return Ok(());
            }
        }

        // staging_in is dnglab's alone (it converts everything it finds); staging_copy holds passthrough files
        let staging_root = paths::staging_dir().join(format!("session-{session_id}"));
        let staging_in = staging_root.join("in");
        let staging_copy = staging_root.join("copy");
        if !request.mark_only {
            std::fs::create_dir_all(&staging_in)?;
            std::fs::create_dir_all(&staging_copy)?;
        }

        let mut to_stage: Vec<Staged> = Vec::new();
        // identical content twice in one batch: recorded as duplicate once the primary's fate is known
        let mut seen_hashes: HashSet<String> = HashSet::new();
        let mut intra_batch_duplicates: Vec<(String, String, i64)> = Vec::new();

        for (i, f) in files.iter().enumerate() {
            self.pause.wait();
            let name = file_name(f);
            self.progress(session_id, PHASE_CHECKING, i + 1, files.len(), &name);
            let order = i as i64;
            let Some(cand) = metadata.get(f) else {
                // iOS swaps transient "IMG_1234D" files for the final shot mid-scan
                if !f.exists() {
                    continue;
                }
                record_file(
                    conn,
                    session_id,
                    &name,
                    "failed",
                    "",
                    "Could not read file metadata",
                    order,
                )?;
                continue;
            };
            if let Some(dest) =
                scanner::quick_duplicate_match(conn, &cand.camera_model, &name, cand.size_bytes)?
            {
                record_file(conn, session_id, &name, "duplicate", &dest, "", order)?;
                continue;
            }
            let staged = if request.mark_only {
                scanner::hash_file(&cand.path).map(|h| (h, None))
            } else {
                let passthrough = scanner::is_dng(&cand.path) || scanner::is_video(&cand.path);
                let stage_dir = if passthrough {
                    &staging_copy
                } else {
                    &staging_in
                };
                let suffix = util::suffix(&cand.path);
                let tmp = stage_dir.join(format!("{order}{suffix}"));
                scanner::hash_and_stage(&cand.path, &tmp).and_then(|h| {
                    let staged = stage_dir.join(format!("{h}{suffix}"));
                    std::fs::rename(&tmp, &staged).map(|_| (h, Some(staged)))
                })
            };
            let (source_hash, staged_path) = match staged {
                Ok(s) => s,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound && !cand.path.exists() => {
                    continue;
                }
                Err(e) => {
                    record_file(conn, session_id, &name, "failed", "", &e.to_string(), order)?;
                    continue;
                }
            };
            if let Some(dest) = scanner::hash_duplicate_check(conn, &source_hash)? {
                if let Some(p) = &staged_path {
                    let _ = std::fs::remove_file(p);
                }
                record_file(conn, session_id, &name, "duplicate", &dest, "", order)?;
                continue;
            }
            if !seen_hashes.insert(source_hash.clone()) {
                // staged copy was just overwritten with identical bytes; the primary's is intact
                intra_batch_duplicates.push((source_hash, name, order));
                continue;
            }
            to_stage.push((source_hash, cand.clone(), order, staged_path));
        }

        if to_stage.is_empty() {
            return self.finish(conn, session_id, "completed", "", root);
        }

        if request.mark_only {
            let now = db::now_utc();
            for (source_hash, cand, order, _) in &to_stage {
                self.pause.wait();
                let name = file_name(&cand.path);
                self.progress(
                    session_id,
                    PHASE_PLACING,
                    *order as usize + 1,
                    to_stage.len(),
                    &name,
                );
                conn.execute(
                    "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, \
                     captured_at, dest_path, dest_bytes, session_id, imported_at) \
                     VALUES (?, ?, ?, ?, ?, '', 0, ?, ?)",
                    params![source_hash, name, cand.size_bytes, cand.camera_model, cand.captured_at, session_id, now],
                )?;
                record_file(conn, session_id, &name, "imported", "", "", *order)?;
            }
            for (_, name, order) in &intra_batch_duplicates {
                record_file(conn, session_id, name, "duplicate", "", "", *order)?;
            }
            return self.finish(conn, session_id, "completed", "", root);
        }

        let staging_out = staging_root.join("out");
        let mut to_convert: Vec<(String, Candidate, i64)> = Vec::new();
        let mut to_copy: Vec<(String, Candidate, i64, PathBuf)> = Vec::new();
        for (source_hash, cand, order, staged) in to_stage {
            if scanner::is_dng(&cand.path) || scanner::is_video(&cand.path) {
                to_copy.push((
                    source_hash,
                    cand,
                    order,
                    staged.expect("staged for real imports"),
                ));
            } else {
                to_convert.push((source_hash, cand, order));
            }
        }
        let names_by_hash: HashMap<&str, String> = to_convert
            .iter()
            .map(|(h, c, _)| (h.as_str(), file_name(&c.path)))
            .collect();

        let compression = db::str_or(settings.get("dng_compression"), "lossless");
        let embed_raw = db::truthy(settings.get("embed_raw_in_dng"));
        let preview_size = db::str_or(settings.get("preview_size"), "medium");
        let total_to_place = to_convert.len() + to_copy.len();

        let mut tail = String::new();
        let mut convert_failures: Vec<(String, i64, String)> = Vec::new();
        if !to_convert.is_empty() {
            self.pause.wait();
            self.progress(session_id, PHASE_CONVERTING, 0, to_convert.len(), "");
            let mut done = 0;
            let result = converter::convert_batch(
                dnglab.as_deref().unwrap(),
                &staging_in,
                &staging_out,
                compression,
                embed_raw,
                preview_size,
                |stem| {
                    done += 1;
                    let display = names_by_hash
                        .get(stem)
                        .cloned()
                        .unwrap_or_else(|| stem.to_string());
                    self.progress(
                        session_id,
                        PHASE_CONVERTING,
                        done,
                        to_convert.len(),
                        display,
                    );
                },
            );
            match result {
                Ok((_, t)) => tail = t,
                Err(e) => {
                    convert_failures = to_convert
                        .drain(..)
                        .map(|(_, c, o)| (file_name(&c.path), o, e.to_string()))
                        .collect();
                }
            }
        }
        for (name, order, err) in &convert_failures {
            record_file(conn, session_id, name, "failed", "", err, *order)?;
        }

        let mut placer = Placer {
            conn,
            session_id,
            library_root: &library_root,
            delete_originals: db::truthy(settings.get("delete_originals_after_import")),
            total: total_to_place,
            placed: 0,
            bytes_saved: 0,
            placed_dest: HashMap::new(),
            pipeline: self,
        };

        for (source_hash, cand, order) in &to_convert {
            self.pause.wait();
            let converted = staging_out.join(format!("{source_hash}.dng"));
            if !converted.is_file() {
                placer.placed += 1;
                let name = file_name(&cand.path);
                self.progress(
                    session_id,
                    PHASE_PLACING,
                    placer.placed,
                    total_to_place,
                    &name,
                );
                let t = if tail.is_empty() {
                    "conversion failed"
                } else {
                    tail.as_str()
                };
                record_file(
                    conn,
                    session_id,
                    &name,
                    "failed",
                    "",
                    last_chars(t.trim(), 300),
                    *order,
                )?;
                continue;
            }
            placer.place(cand, *order, source_hash, &converted, true)?;
        }
        for (source_hash, cand, order, staged) in &to_copy {
            self.pause.wait();
            placer.place(cand, *order, source_hash, staged, false)?;
        }
        for (source_hash, name, order) in &intra_batch_duplicates {
            let dest = placer
                .placed_dest
                .get(source_hash)
                .cloned()
                .unwrap_or_default();
            record_file(conn, session_id, name, "duplicate", &dest, "", *order)?;
        }

        let _ = std::fs::remove_dir_all(&staging_root);
        conn.execute(
            "UPDATE sessions SET bytes_saved = bytes_saved + ? WHERE id = ?",
            params![placer.bytes_saved, session_id],
        )?;
        self.finish(conn, session_id, "completed", "", root)
    }

    fn finish(
        &self,
        conn: &Connection,
        session_id: i64,
        status: &str,
        error: &str,
        ejectable: &str,
    ) -> anyhow::Result<()> {
        conn.execute(
            "UPDATE sessions SET status = ?, finished_at = ?, error_message = ?, ejectable_path = ? WHERE id = ?",
            params![status, db::now_utc(), error, ejectable, session_id],
        )?;
        (self.emit)(Event::Finished(session_id));
        Ok(())
    }
}

fn last_chars(s: &str, n: usize) -> &str {
    let count = s.chars().count();
    match s.char_indices().nth(count.saturating_sub(n)) {
        Some((i, _)) if count > n => &s[i..],
        _ => s,
    }
}

struct Placer<'a> {
    conn: &'a Connection,
    session_id: i64,
    library_root: &'a Path,
    delete_originals: bool,
    total: usize,
    placed: usize,
    bytes_saved: i64,
    /// source_hash -> placed dest, for recording intra-batch duplicates against it
    placed_dest: HashMap<String, String>,
    pipeline: &'a Pipeline,
}

impl Placer<'_> {
    fn place(
        &mut self,
        cand: &Candidate,
        order: i64,
        source_hash: &str,
        produced: &Path,
        converted: bool,
    ) -> anyhow::Result<()> {
        let (conn, session_id) = (self.conn, self.session_id);
        let name = file_name(&cand.path);
        self.placed += 1;
        self.pipeline
            .progress(session_id, PHASE_PLACING, self.placed, self.total, &name);
        let dest_suffix = util::suffix(produced).to_lowercase();
        let dest = converter::unique_dest_path(converter::library_dest_path(
            self.library_root,
            cand,
            &dest_suffix,
        )?);
        let moved = dest
            .parent()
            .map_or(Ok(()), std::fs::create_dir_all)
            .and_then(|_| util::move_file(produced, &dest));
        if let Err(e) = moved {
            return record_file(conn, session_id, &name, "failed", "", &e.to_string(), order);
        }

        if dest_suffix == ".dng" {
            converter::set_dng_backward_version(&dest, converted.then_some(name.as_str()));
        } else if cand.camera_model_inferred && scanner::is_video(&dest) {
            converter::set_camera_model(&dest, &cand.camera_model);
        }
        let dest_bytes = dest.metadata()?.len() as i64;
        let dest_str = lossy(&dest);
        let inserted = conn.execute(
            "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, \
             captured_at, dest_path, dest_bytes, session_id, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                source_hash,
                name,
                cand.size_bytes,
                cand.camera_model,
                cand.captured_at,
                dest_str,
                dest_bytes,
                session_id,
                db::now_utc()
            ],
        );
        if let Err(rusqlite::Error::SqliteFailure(e, _)) = &inserted
            && e.code == ErrorCode::ConstraintViolation
        {
            // another session imported this content since our dedup check: drop our copy
            let _ = std::fs::remove_file(&dest);
            let existing = scanner::hash_duplicate_check(conn, source_hash)?.unwrap_or_default();
            self.placed_dest
                .insert(source_hash.into(), existing.clone());
            return record_file(conn, session_id, &name, "duplicate", &existing, "", order);
        }
        inserted?;

        self.bytes_saved += (cand.size_bytes - dest_bytes).max(0);
        self.placed_dest
            .insert(source_hash.into(), dest_str.clone());
        record_file(conn, session_id, &name, "imported", &dest_str, "", order)?;

        if self.delete_originals
            && let Err(e) = std::fs::remove_file(&cand.path)
        {
            log::info!(
                "Could not delete original {} after import: {e}",
                cand.path.display()
            );
        }
        Ok(())
    }
}

fn record_file(
    conn: &Connection,
    session_id: i64,
    filename: &str,
    status: &str,
    dest_path: &str,
    error: &str,
    sort_order: i64,
) -> anyhow::Result<()> {
    let column = match status {
        "imported" => "imported_count",
        "duplicate" => "duplicate_count",
        _ => "failed_count",
    };
    let tx = conn.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO session_files (session_id, source_filename, status, dest_path, error_message, sort_order) \
         VALUES (?, ?, ?, ?, ?, ?)",
        params![session_id, filename, status, dest_path, error, sort_order],
    )?;
    tx.execute(
        &format!("UPDATE sessions SET {column} = {column} + 1 WHERE id = ?"),
        [session_id],
    )?;
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests;
