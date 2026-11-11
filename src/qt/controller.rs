//! `AppController`: the single QML-facing facade. Owns the GUI-thread DB connection and every worker;
//! worker/device/D-Bus callbacks are queued onto the Qt thread.
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::process::Command;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime};

use cxx_qt::{CxxQtThread, CxxQtType, Threading};
use cxx_qt_lib::{QList, QString, QUrl, QVariant};
use ksni::blocking::{Handle, TrayMethods};
use rusqlite::{Connection, params};
use serde_json::{Value, json};

use super::bridge::qobject::{AppController, SessionListModel, SourceListModel};
use super::convert::{from_qvariant, strings, to_qvariant};
use crate::desktop::{self, Inhibitor, Notifier, Portal, PortalChange};
use crate::devices::{self, DeviceEvent, DeviceWatcher};
use crate::import::{self, Event, ImportRequest, ImportWorker};
use crate::workers::{self, PreviewWorker, StatsWorker};
use crate::{db, paths, scanner, util};

type Qt = CxxQtThread<AppController>;

struct Runtime {
    watcher: mpsc::Sender<WatchCmd>,
    watcher_thread: Option<JoinHandle<()>>,
    import: ImportWorker,
    preview: PreviewWorker,
    stats: StatsWorker,
    notifier: Notifier,
    _portal: Portal,
    tray: Option<Handle<Tray>>,
}

impl Drop for Runtime {
    fn drop(&mut self) {
        let _ = self.watcher.send(WatchCmd::Stop);
        if let Some(t) = self.watcher_thread.take() {
            let _ = t.join();
        }
        self.import.stop();
        self.preview.stop();
        self.stats.stop();
        if let Some(t) = self.tray.take() {
            t.shutdown().wait();
        }
    }
}

pub struct Controller {
    pub(crate) session_model: *mut SessionListModel,
    pub(crate) sources_model: *mut SourceListModel,
    pub(crate) active_session_id: i32,
    pub(crate) active_label: QString,
    pub(crate) active_phase: QString,
    pub(crate) active_done: i32,
    pub(crate) active_total: i32,
    pub(crate) active_file: QString,
    pub(crate) active_eta_seconds: i32,
    pub(crate) queued_count: i32,
    pub(crate) imports_paused: bool,
    pub(crate) system_prefers_dark: bool,
    pub(crate) dnglab_ready: bool,
    pub(crate) watch_enabled: bool,
    pub(crate) theme_accent: QString,
    pub(crate) theme_custom: QVariant,
    pub(crate) start_hidden: bool,
    pub(crate) app_version: QString,
    // field order is drop order: workers stop before the connection closes
    rt: Option<Runtime>,
    conn: Option<Connection>,
    phase_started: (String, Instant, i32),
    /// source roots with a queued or running session
    in_flight_roots: HashSet<String>,
    inhibitor: Inhibitor,
}

impl Default for Controller {
    fn default() -> Self {
        Self {
            session_model: std::ptr::null_mut(),
            sources_model: std::ptr::null_mut(),
            active_session_id: -1,
            active_label: QString::default(),
            active_phase: QString::default(),
            active_done: 0,
            active_total: 0,
            active_file: QString::default(),
            active_eta_seconds: -1,
            queued_count: 0,
            imports_paused: false,
            system_prefers_dark: false,
            dnglab_ready: false,
            watch_enabled: true,
            theme_accent: QString::default(),
            theme_custom: to_qvariant(&json!({})),
            start_hidden: crate::args().start_hidden,
            app_version: QString::from(env!("CARGO_PKG_VERSION")),
            rt: None,
            conn: None,
            phase_started: (String::new(), Instant::now(), 0),
            in_flight_roots: HashSet::new(),
            inhibitor: Inhibitor::default(),
        }
    }
}

fn qs(s: &str) -> QString {
    QString::from(s)
}

fn local_path(url_or_path: &QString) -> String {
    QUrl::from(url_or_path)
        .to_local_file()
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| url_or_path.to_string())
}

fn queue(qt: &Qt, f: impl FnOnce(Pin<&mut AppController>) + Send + 'static) {
    let _ = qt.queue(f);
}

fn load_theme_json() -> Value {
    std::fs::read(paths::theme_file())
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(json!({}))
}

fn theme_stamp() -> Option<(SystemTime, u64)> {
    std::fs::metadata(paths::theme_file())
        .ok()
        .map(|m| (m.modified().unwrap_or(SystemTime::UNIX_EPOCH), m.len()))
}

impl cxx_qt::Initialize for AppController {
    fn initialize(mut self: Pin<&mut Self>) {
        let mut conn = match db::connect() {
            Ok(c) => c,
            Err(e) => return log::error!("cannot open database: {e}"),
        };
        // nothing resumes a 'running' session from a previous process
        let _ = conn.execute(
            "UPDATE sessions SET status = 'failed', finished_at = ?, \
             error_message = 'Import was interrupted (app restarted or crashed)' WHERE status = 'running'",
            [db::now_utc()],
        );
        if crate::args().demo
            && let Err(e) = db::seed_demo_if_empty(&mut conn)
        {
            log::warn!("demo seed failed: {e}");
        }
        let watch = db::truthy(
            db::all_settings(&conn)
                .ok()
                .as_ref()
                .and_then(|s| s.get("watch_enabled")),
        );
        let mut r = self.as_mut().rust_mut();
        r.watch_enabled = watch;
        r.dnglab_ready = import::find_dnglab().is_some();
        r.conn = Some(conn);
    }
}

impl AppController {
    fn db(&self) -> Option<&Connection> {
        self.conn.as_ref()
    }

    fn sessions(&self) -> Option<Pin<&'static mut SessionListModel>> {
        // SAFETY: both models are QML siblings of this controller and outlive every call made
        // while the event loop runs; the pointer is null until QML assigns it.
        unsafe { self.session_model.as_mut().map(|m| Pin::new_unchecked(m)) }
    }

    fn sources(&self) -> Option<Pin<&'static mut SourceListModel>> {
        // SAFETY: as in `sessions`
        unsafe { self.sources_model.as_mut().map(|m| Pin::new_unchecked(m)) }
    }

    fn source_entries(&self) -> Vec<serde_json::Map<String, Value>> {
        self.sources().map(|s| s.all_entries()).unwrap_or_default()
    }

    fn request_stats(&self, key: &str, root: &str) {
        if let Some(rt) = &self.rt {
            rt.stats.request(key, root);
        }
    }

    fn emit_toast(self: Pin<&mut Self>, text: &str) {
        self.toast(qs(text));
    }

    pub fn start(mut self: Pin<&mut Self>) {
        if self.rt.is_some() || self.conn.is_none() {
            return;
        }
        let qt = self.qt_thread();
        if let (Some(sessions), Some(conn)) = (self.sessions(), self.db()) {
            sessions.load(conn);
        }
        let folders: Vec<(i64, String, String)> = self
            .db()
            .and_then(|c| {
                c.prepare("SELECT id, label, path FROM saved_folders ORDER BY sort_order")
                    .and_then(|mut s| {
                        s.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
                            .collect()
                    })
                    .ok()
            })
            .unwrap_or_default();
        if let Some(mut sources) = self.sources() {
            for (id, label, path) in &folders {
                sources
                    .as_mut()
                    .upsert(&format!("folder:{id}"), label, "folder", true, path, true);
            }
        }

        let portal = {
            let qt = qt.clone();
            Portal::new(move |change| queue(&qt, move |c| c.on_portal_change(change)))
        };
        let dark = portal.color_scheme() == desktop::PREFER_DARK;
        let accent = portal.accent();
        let _ = std::fs::create_dir_all(paths::theme_file().parent().unwrap());
        let theme = load_theme_json();
        {
            let mut r = self.as_mut().rust_mut();
            r.system_prefers_dark = dark;
            r.theme_accent = qs(&accent);
            r.theme_custom = to_qvariant(&theme);
        }
        self.as_mut().system_prefers_dark_changed();
        self.as_mut().palette_changed();

        let import = {
            let qt = qt.clone();
            ImportWorker::start(move |ev| queue(&qt, move |c| c.on_import_event(ev)))
        };
        let preview = {
            let qt = qt.clone();
            workers::start_preview(move |key, items| {
                queue(&qt, move |c| c.preview_ready(qs(&key), to_qvariant(&items)));
            })
        };
        let stats = {
            let qt = qt.clone();
            workers::start_stats(move |key, stats| {
                queue(&qt, move |c| {
                    if let Some(s) = c.sources() {
                        s.set_stats(&key, stats);
                    }
                });
            })
        };
        let notifier = {
            let qt = qt.clone();
            Notifier::new(move |session_id| queue(&qt, move |c| c.on_notification_open(session_id)))
        };

        let mtp_enabled = self
            .db()
            .is_none_or(|c| db::truthy(Some(&db::get_setting(c, "mtp_enabled"))));
        let (watcher, cmds) = mpsc::channel::<WatchCmd>();
        let watcher_thread = {
            let qt = qt.clone();
            std::thread::spawn(move || run_watcher(&qt, mtp_enabled, &cmds))
        };

        let tray = Tray { qt: qt.clone() }
            .spawn()
            .map_err(|e| log::info!("no system tray: {e}"))
            .ok();
        spawn_instance_listener(&qt);
        spawn_watchdog(&qt);
        crate::sleep::spawn();

        self.as_mut().rust_mut().rt = Some(Runtime {
            watcher,
            watcher_thread: Some(watcher_thread),
            import,
            preview,
            stats,
            notifier,
            _portal: portal,
            tray,
        });
        for (id, _, path) in folders {
            self.request_stats(&format!("folder:{id}"), &path);
        }
    }

    // --- device detection / import sessions ---

    fn on_device_event(mut self: Pin<&mut Self>, ev: DeviceEvent) {
        match ev {
            DeviceEvent::SourceFound { root, label, kind } => {
                self.on_source_found(root, label, kind)
            }
            DeviceEvent::SourceAdded {
                key,
                label,
                kind,
                mounted,
                root,
            } => {
                if let Some(s) = self.sources() {
                    s.upsert(&key, &label, &kind, mounted, &root, false);
                }
                if mounted {
                    self.request_stats(&key, &root);
                }
            }
            DeviceEvent::SourceRemoved(key) => {
                if let Some(s) = self.sources() {
                    s.remove(&key);
                }
            }
            DeviceEvent::MountChanged { key, mounted, root } => {
                if let Some(s) = self.sources() {
                    s.set_mounted(&key, mounted, &root);
                }
                if mounted {
                    self.as_mut().request_stats(&key, &root);
                }
            }
        }
    }

    pub fn refresh_devices(self: Pin<&mut Self>) {
        let Some(rt) = &self.rt else { return };
        let _ = rt.watcher.send(WatchCmd::Poll);
        // poll only reports changes; a mounted card may still have new photos
        for e in self.source_entries() {
            let root = e["rootPath"].as_str().unwrap_or("");
            if e["mounted"] == json!(true) && !root.is_empty() {
                self.request_stats(e["sourceKey"].as_str().unwrap_or(""), root);
            }
        }
    }

    fn on_source_found(self: Pin<&mut Self>, root: String, label: String, kind: String) {
        if !self
            .db()
            .is_some_and(|c| db::truthy(Some(&db::get_setting(c, "watch_enabled"))))
        {
            return;
        }
        if let Some(rt) = &self.rt {
            // transient: fires on every plug-in
            rt.notifier.send(
                "Photo Spool",
                &format!("{label}: importing new photos…"),
                None,
                true,
                -1,
            );
        }
        self.submit_import(ImportRequest {
            source_root: root,
            device_label: label,
            kind,
            ..Default::default()
        });
    }

    fn submit_import(mut self: Pin<&mut Self>, request: ImportRequest) -> bool {
        if self.in_flight_roots.contains(&request.source_root) {
            self.emit_toast(&format!("{} is already importing.", request.device_label));
            return false;
        }
        let mut r = self.as_mut().rust_mut();
        r.in_flight_roots.insert(request.source_root.clone());
        r.inhibitor.begin();
        r.queued_count += 1;
        if let Some(rt) = &r.rt {
            rt.import.submit(request);
        }
        self.active_session_changed();
        true
    }

    /// Quiet: never touches the status bar or queued count.
    fn submit_mark_only(mut self: Pin<&mut Self>, request: ImportRequest) -> bool {
        if self.in_flight_roots.contains(&request.source_root) {
            self.emit_toast(&format!("{} is already busy.", request.device_label));
            return false;
        }
        let mut r = self.as_mut().rust_mut();
        r.in_flight_roots.insert(request.source_root.clone());
        r.inhibitor.begin();
        if let Some(rt) = &r.rt {
            rt.import.submit(request);
        }
        true
    }

    fn on_import_event(self: Pin<&mut Self>, ev: Event) {
        match ev {
            Event::Started(id) => self.on_session_started(id),
            Event::Progress {
                session_id,
                phase,
                done,
                total,
                file,
            } => self.on_session_progress(session_id, phase, done, total, file),
            Event::Finished(id) => self.on_session_finished(id),
            Event::DnglabUnavailable(_) => self.emit_toast(
                "The DNG converter (dnglab) isn't installed -- install the dnglab package.",
            ),
        }
    }

    fn reset_active(mut self: Pin<&mut Self>, id: i32, label: &str) {
        let mut r = self.as_mut().rust_mut();
        r.active_session_id = id;
        r.active_label = qs(label);
        r.active_phase = QString::default();
        r.active_done = 0;
        r.active_total = 0;
        r.active_file = QString::default();
        r.active_eta_seconds = -1;
        r.phase_started = (String::new(), Instant::now(), 0);
        self.active_session_changed();
    }

    fn on_session_started(mut self: Pin<&mut Self>, session_id: i64) {
        let Some(conn) = self.db() else { return };
        let row: Option<(String, bool)> = conn
            .query_row(
                "SELECT device_label, mark_only FROM sessions WHERE id = ?",
                [session_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .ok();
        let Some((label, false)) = row else { return };
        if let Some(s) = self.sessions() {
            s.upsert(session_id, conn);
        }
        let q = (self.queued_count - 1).max(0);
        self.as_mut().rust_mut().queued_count = q;
        self.reset_active(session_id as i32, &label);
    }

    fn on_session_progress(
        mut self: Pin<&mut Self>,
        session_id: i64,
        phase: &str,
        done: i64,
        total: i64,
        file: String,
    ) {
        if let Some(s) = self.sessions() {
            s.set_progress(session_id, phase, done, total, &file);
        }
        if session_id as i32 != self.active_session_id {
            return;
        }
        let eta = self.as_mut().estimate_eta(phase, done as i32, total as i32);
        let mut r = self.as_mut().rust_mut();
        r.active_phase = qs(phase);
        r.active_done = done as i32;
        r.active_total = total as i32;
        r.active_file = qs(&file);
        r.active_eta_seconds = eta;
        self.active_session_changed();
    }

    fn estimate_eta(self: Pin<&mut Self>, phase: &str, done: i32, total: i32) -> i32 {
        if phase != self.phase_started.0 {
            self.rust_mut().phase_started = (phase.into(), Instant::now(), done);
            return -1;
        }
        let (_, started, done_at_start) = self.phase_started;
        let elapsed = started.elapsed().as_secs_f64();
        let progressed = done - done_at_start;
        if elapsed < 3.0 || progressed <= 0 || total <= done {
            return -1;
        }
        ((total - done) as f64 * elapsed / progressed as f64).round() as i32
    }

    fn on_session_finished(mut self: Pin<&mut Self>, session_id: i64) {
        let Some(conn) = self.db() else { return };
        type Row = (String, String, bool, String, String, i64, i64, i64);
        let row: Option<Row> = conn
            .query_row(
                "SELECT source_root, device_label, mark_only, status, error_message, imported_count, duplicate_count, \
                 failed_count FROM sessions WHERE id = ?",
                [session_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?)),
            )
            .ok();
        let Some((root, label, mark_only, status, error, imported, duplicates, failed)) = row
        else {
            return;
        };
        {
            let mut r = self.as_mut().rust_mut();
            r.in_flight_roots.remove(&root);
            r.inhibitor.end();
        }
        // refresh the card's "N new" count for real imports and quiet mark-as-owned alike
        if !root.is_empty() {
            for e in self.source_entries() {
                if e["mounted"] == json!(true) && e["rootPath"].as_str() == Some(root.as_str()) {
                    self.request_stats(e["sourceKey"].as_str().unwrap_or(""), &root);
                }
            }
        }
        if mark_only {
            return;
        }
        if let (Some(s), Some(conn)) = (self.sessions(), self.db()) {
            s.upsert(session_id, conn);
        }
        if session_id as i32 == self.active_session_id {
            self.as_mut().reset_active(-1, "");
        }
        let text = if status == "failed" {
            format!(
                "{label}: import failed -- {}",
                if error.is_empty() {
                    "unknown error"
                } else {
                    &error
                }
            )
        } else if imported > 0 || duplicates > 0 {
            let mut parts = vec![format!("{imported} imported")];
            if duplicates > 0 {
                parts.push(format!("{duplicates} duplicates skipped"));
            }
            if failed > 0 {
                parts.push(format!("{failed} failed"));
            }
            format!("{label}: {}.", parts.join(", "))
        } else {
            return;
        };
        let notify = self
            .db()
            .is_some_and(|c| db::truthy(Some(&db::get_setting(c, "notify_on_complete"))));
        if let (true, Some(rt)) = (notify, &self.rt) {
            let transient = status != "failed" && imported == 0 && failed == 0;
            rt.notifier
                .send("Photo Spool", &text, Some(session_id), transient, -1);
        }
    }

    /// Excludes duplicates: the detail view shows what an import did, not every file it saw.
    pub fn get_session_files(&self, session_id: i32) -> QVariant {
        let rows: Vec<Value> = self
            .db()
            .and_then(|c| {
                c.prepare(
                    "SELECT id, source_filename, status, dest_path, error_message FROM session_files \
                     WHERE session_id = ? AND status != 'duplicate' ORDER BY sort_order",
                )
                .and_then(|mut s| {
                    s.query_map([session_id], |r| {
                        Ok(json!({
                            "id": r.get::<_, i64>(0)?,
                            "sourceFilename": r.get::<_, String>(1)?,
                            "status": r.get::<_, String>(2)?,
                            "destPath": r.get::<_, String>(3)?,
                            "errorMessage": r.get::<_, String>(4)?,
                        }))
                    })?
                    .collect()
                })
                .ok()
            })
            .unwrap_or_default();
        to_qvariant(&Value::Array(rows))
    }

    /// Lifetime stats from the dedup ledger (survives clearing history); empty dest_path = marked only.
    pub fn get_library_stats(&self) -> QVariant {
        let Some(c) = self.db() else {
            return QVariant::default();
        };
        let stats = (|| -> rusqlite::Result<Value> {
            let (n, dest, src, earliest, latest): (i64, i64, i64, Option<String>, Option<String>) = c.query_row(
                "SELECT COUNT(*), COALESCE(SUM(dest_bytes), 0), COALESCE(SUM(source_bytes), 0), MIN(captured_at), \
                 MAX(captured_at) FROM imports WHERE dest_path != ''",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )?;
            let marked: i64 = c.query_row(
                "SELECT COUNT(*) FROM imports WHERE dest_path = ''",
                [],
                |r| r.get(0),
            )?;
            let by_model: Vec<Value> = c
                .prepare(
                    "SELECT camera_model, COUNT(*) AS n, COALESCE(SUM(dest_bytes), 0) FROM imports \
                     WHERE dest_path != '' GROUP BY camera_model ORDER BY n DESC",
                )?
                .query_map([], |r| {
                    let model: String = r.get(0)?;
                    Ok(json!({
                        "model": if model.is_empty() { "Unknown".into() } else { model },
                        "count": r.get::<_, i64>(1)?,
                        "bytes": r.get::<_, i64>(2)?,
                    }))
                })?
                .collect::<rusqlite::Result<_>>()?;
            let videos = c
                .prepare("SELECT dest_path FROM imports WHERE dest_path != ''")?
                .query_map([], |r| r.get::<_, String>(0))?
                .filter(|p| p.as_ref().is_ok_and(|p| scanner::is_video(Path::new(p))))
                .count() as i64;
            Ok(json!({
                "totalCount": n,
                "totalBytes": dest,
                "bytesSaved": (src - dest).max(0),
                "earliestCapturedAt": earliest.unwrap_or_default(),
                "latestCapturedAt": latest.unwrap_or_default(),
                "photoCount": n - videos,
                "videoCount": videos,
                "markedOnlyCount": marked,
                "byModel": by_model,
            }))
        })();
        stats.map(|s| to_qvariant(&s)).unwrap_or_else(|e| {
            log::warn!("library stats: {e}");
            QVariant::default()
        })
    }

    pub fn open_file(self: Pin<&mut Self>, dest_path: QString) {
        let path = dest_path.to_string();
        if path.is_empty() {
            return;
        }
        if util::spawn_detached(Command::new("xdg-open").arg(&path)).is_err() {
            self.emit_toast("Could not open the file.");
        }
    }

    /// FileManager1.ShowItems selects the file; falls back to opening its folder.
    pub fn reveal_in_file_browser(self: Pin<&mut Self>, dest_path: QString) {
        let path = dest_path.to_string();
        if path.is_empty() {
            return;
        }
        if desktop::show_in_file_manager(
            &QUrl::from_local_file(&dest_path).to_qstring().to_string(),
        ) {
            return;
        }
        let folder = Path::new(&path)
            .parent()
            .map(util::lossy)
            .unwrap_or_default();
        if util::spawn_detached(Command::new("xdg-open").arg(folder)).is_err() {
            self.emit_toast("Could not open the file browser.");
        }
    }

    pub fn eject_source(self: Pin<&mut Self>, source_key: QString) {
        let Some(e) = self
            .sources()
            .and_then(|s| s.entry_for(&source_key.to_string()))
        else {
            return;
        };
        let root = e["rootPath"].as_str().unwrap_or("").to_string();
        if root.is_empty() {
            return;
        }
        if self.in_flight_roots.contains(&root) {
            let label = e["label"].as_str().unwrap_or("");
            return self.emit_toast(&format!("{label} is still importing."));
        }
        if let Some(rt) = &self.rt {
            let _ = rt.watcher.send(WatchCmd::Eject(root));
        }
    }

    /// Forgets the history entry only; imported photos stay on disk.
    pub fn clear_session(self: Pin<&mut Self>, session_id: i32) {
        if session_id == self.active_session_id {
            return;
        }
        if let Some(c) = self.db() {
            let _ = c.execute("DELETE FROM sessions WHERE id = ?", [session_id]);
        }
        if let Some(s) = self.sessions() {
            s.remove(session_id as i64);
        }
    }

    pub fn clear_history(self: Pin<&mut Self>) {
        let keep = self.active_session_id;
        if let Some(c) = self.db() {
            // running mark-only sessions too: their rows must outlive the run
            let _ = c.execute(
                "DELETE FROM sessions WHERE id != ? AND status != 'running'",
                [keep],
            );
        }
        if let Some(s) = self.sessions() {
            s.remove_all_except(keep as i64);
        }
    }

    // --- source strip: live devices + saved folders ---

    pub fn add_folder(self: Pin<&mut Self>, folder_url: QString) {
        let path = local_path(&folder_url);
        if path.is_empty() {
            return;
        }
        let name = util::file_name(Path::new(&path));
        let label = if name.is_empty() { path.clone() } else { name };
        let Some(c) = self.db() else { return };
        let next: i64 = c
            .query_row(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM saved_folders",
                [],
                |r| r.get(0),
            )
            .unwrap_or(0);
        let inserted = c.execute(
            "INSERT INTO saved_folders (path, label, sort_order, created_at) VALUES (?, ?, ?, ?)",
            params![path, label, next, db::now_utc()],
        );
        if inserted.is_err() {
            return self.emit_toast("That folder is already added.");
        }
        let key = format!("folder:{}", c.last_insert_rowid());
        if let Some(s) = self.sources() {
            s.upsert(&key, &label, "folder", true, &path, true);
        }
        self.request_stats(&key, &path);
    }

    pub fn remove_folder(self: Pin<&mut Self>, source_key: QString) {
        let key = source_key.to_string();
        let Some(id) = key
            .strip_prefix("folder:")
            .and_then(|i| i.parse::<i64>().ok())
        else {
            return;
        };
        if let Some(c) = self.db() {
            let _ = c.execute("DELETE FROM saved_folders WHERE id = ?", [id]);
        }
        if let Some(s) = self.sources() {
            s.remove(&key);
        }
    }

    /// Mounts an unmounted card first: the click is the mount prompt.
    pub fn request_preview(self: Pin<&mut Self>, source_key: QString) {
        let key = source_key.to_string();
        let Some(e) = self.sources().and_then(|s| s.entry_for(&key)) else {
            return self.preview_failed(source_key, qs("That source is no longer available."));
        };
        let root = e["rootPath"].as_str().unwrap_or("").to_string();
        match (&self.rt, root.is_empty()) {
            (Some(rt), true) if e["kind"] == json!("blockdev") => {
                let _ = rt.watcher.send(WatchCmd::Mount(key));
            }
            (Some(rt), false) => rt.preview.request(&key, &root),
            _ => self.preview_failed(source_key, qs("Could not mount this device.")),
        }
    }

    fn on_mounted_for_preview(self: Pin<&mut Self>, key: String, root: Option<String>) {
        match (&self.rt, root) {
            (Some(rt), Some(root)) => rt.preview.request(&key, &root),
            _ => self.preview_failed(qs(&key), qs("Could not mount this device.")),
        }
    }

    fn resolve_selection(
        self: Pin<&mut Self>,
        source_key: &QString,
        names: &QList<QVariant>,
    ) -> Option<(String, String, String, HashSet<String>)> {
        let Some(e) = self
            .sources()
            .and_then(|s| s.entry_for(&source_key.to_string()))
        else {
            self.emit_toast("That source is no longer available.");
            return None;
        };
        let root = e["rootPath"].as_str().unwrap_or("").to_string();
        if root.is_empty() {
            self.emit_toast("That source is no longer available.");
            return None;
        }
        let names: HashSet<String> = strings(names).into_iter().collect();
        if names.is_empty() {
            return None;
        }
        // "iphone" is display-only; sessions.kind only allows blockdev/mtp/manual
        let kind = match e["kind"].as_str().unwrap_or("") {
            "folder" => "manual",
            "iphone" => "mtp",
            k => k,
        };
        Some((
            root,
            e["label"].as_str().unwrap_or("").to_string(),
            kind.into(),
            names,
        ))
    }

    pub fn import_selected(
        mut self: Pin<&mut Self>,
        source_key: QString,
        filenames: QList<QVariant>,
    ) {
        let Some((root, label, kind, names)) =
            self.as_mut().resolve_selection(&source_key, &filenames)
        else {
            return;
        };
        self.submit_import(ImportRequest {
            source_root: root,
            device_label: label,
            kind,
            selected_filenames: Some(names),
            mark_only: false,
        });
    }

    /// Records files in the dedup ledger without copying anything (e.g. already in Lightroom).
    pub fn mark_already_imported(
        mut self: Pin<&mut Self>,
        source_key: QString,
        filenames: QList<QVariant>,
    ) {
        let Some((root, label, kind, names)) =
            self.as_mut().resolve_selection(&source_key, &filenames)
        else {
            return;
        };
        self.submit_mark_only(ImportRequest {
            source_root: root,
            device_label: format!("{label} (marked, not imported)"),
            kind,
            selected_filenames: Some(names),
            mark_only: true,
        });
    }

    /// Forgets ledger entries for exact paths; runs on the GUI thread, so never rescans the source.
    pub fn unmark_imported(
        mut self: Pin<&mut Self>,
        source_key: QString,
        file_paths: QList<QVariant>,
    ) {
        let files: Vec<PathBuf> = strings(&file_paths)
            .into_iter()
            .filter(|p| !p.is_empty())
            .map(PathBuf::from)
            .collect();
        let Some(c) = self.db() else { return };
        if files.is_empty() {
            return;
        }
        let metadata = scanner::read_metadata(&files, Some(c));
        let mut removed = 0;
        if let Ok(tx) = c.unchecked_transaction() {
            for f in &files {
                let Some(cand) = metadata.get(f) else {
                    continue;
                };
                removed += tx
                    .execute(
                        "DELETE FROM imports WHERE camera_model = ? AND source_filename = ? AND source_bytes = ?",
                        params![cand.camera_model, util::file_name(f), cand.size_bytes],
                    )
                    .unwrap_or(0);
            }
            let _ = tx.commit();
        }
        let key = source_key.to_string();
        let root = self
            .sources()
            .and_then(|s| s.entry_for(&key))
            .and_then(|e| e["rootPath"].as_str().map(String::from));
        if removed > 0 {
            let plural = if removed != 1 { "s" } else { "" };
            self.as_mut().emit_toast(&format!(
                "Unmarked {removed} file{plural} -- they'll show as new again."
            ));
        }
        if let Some(root) = root.filter(|r| !r.is_empty()) {
            self.request_stats(&key, &root);
        }
    }

    fn on_notification_open(self: Pin<&mut Self>, session_id: i64) {
        let label: String = self
            .db()
            .and_then(|c| {
                c.query_row(
                    "SELECT device_label FROM sessions WHERE id = ?",
                    [session_id],
                    |r| r.get(0),
                )
                .ok()
            })
            .unwrap_or_default();
        self.navigate_to_session(session_id as i32, qs(&label));
    }

    pub fn set_imports_paused(mut self: Pin<&mut Self>, paused: bool) {
        let mut r = self.as_mut().rust_mut();
        r.imports_paused = paused;
        r.phase_started = (String::new(), Instant::now(), 0);
        if let Some(rt) = &r.rt {
            rt.import.set_paused(paused);
        }
        self.imports_paused_changed();
    }

    fn on_portal_change(mut self: Pin<&mut Self>, change: PortalChange) {
        match change {
            PortalChange::ColorScheme(v) => {
                let dark = v == desktop::PREFER_DARK;
                if dark != self.system_prefers_dark {
                    self.as_mut().rust_mut().system_prefers_dark = dark;
                    self.system_prefers_dark_changed();
                }
            }
            PortalChange::Accent(a) => {
                self.as_mut().rust_mut().theme_accent = qs(&a);
                self.palette_changed();
            }
        }
    }

    pub fn retry_dnglab_setup(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().dnglab_ready = import::find_dnglab().is_some();
        self.dnglab_ready_changed();
    }

    pub fn get_setting(&self, key: QString) -> QVariant {
        self.db()
            .map(|c| to_qvariant(&db::get_setting(c, &key.to_string())))
            .unwrap_or_default()
    }

    pub fn set_setting(mut self: Pin<&mut Self>, key: QString, value: QVariant) {
        let key = key.to_string();
        let value = from_qvariant(&value);
        if let Some(c) = self.db()
            && let Err(e) = db::set_setting(c, &key, &value)
        {
            log::warn!("saving setting {key}: {e}");
        }
        match key.as_str() {
            "watch_enabled" => {
                self.as_mut().rust_mut().watch_enabled = db::truthy(Some(&value));
                self.watch_enabled_changed();
            }
            "mtp_enabled" => {
                if let Some(rt) = &self.rt {
                    let _ = rt.watcher.send(WatchCmd::SetMtp(db::truthy(Some(&value))));
                }
            }
            _ => {}
        }
    }

    pub fn default_library_root(&self) -> QString {
        qs(&util::lossy(&paths::default_library_root()))
    }

    pub fn set_library_root(self: Pin<&mut Self>, folder_url: QString) {
        let path = local_path(&folder_url);
        if let (false, Some(c)) = (path.is_empty(), self.db()) {
            let _ = db::set_setting(c, "library_root", &json!(path));
        }
    }
}

enum WatchCmd {
    Poll,
    Eject(String),
    Mount(String),
    SetMtp(bool),
    Stop,
}

/// Owns the device watcher so its blocking D-Bus/gio/FUSE calls never touch the GUI thread.
fn run_watcher(qt: &Qt, mtp_enabled: bool, cmds: &mpsc::Receiver<WatchCmd>) {
    let mut watcher = {
        let qt = qt.clone();
        DeviceWatcher::new(mtp_enabled, move |ev| {
            queue(&qt, move |c| c.on_device_event(ev))
        })
    };
    let mut theme = theme_stamp();
    watcher.poll();
    loop {
        match cmds.recv_timeout(devices::POLL_INTERVAL) {
            Ok(WatchCmd::Poll) | Err(RecvTimeoutError::Timeout) => watcher.poll(),
            Ok(WatchCmd::Eject(root)) => {
                if !watcher.eject(&root) {
                    queue(qt, |c| {
                        c.emit_toast("Could not eject -- it may still be in use.")
                    });
                }
            }
            Ok(WatchCmd::Mount(key)) => {
                let root = watcher.mount_source(&key);
                queue(qt, move |c| c.on_mounted_for_preview(key, root));
            }
            Ok(WatchCmd::SetMtp(on)) => watcher.set_mtp_enabled(on),
            Ok(WatchCmd::Stop) | Err(RecvTimeoutError::Disconnected) => return watcher.stop(),
        }
        // ponytail: theme.json polled on the device tick; inotify if 2.5s lag ever matters
        let now = theme_stamp();
        if now != theme {
            theme = now;
            let custom = load_theme_json();
            queue(qt, move |mut c| {
                c.as_mut().rust_mut().theme_custom = to_qvariant(&custom);
                c.palette_changed();
            });
        }
    }
}

pub struct Tray {
    qt: Qt,
}

impl ksni::Tray for Tray {
    fn id(&self) -> String {
        "photo-spool".into()
    }

    fn title(&self) -> String {
        "Photo Spool".into()
    }

    fn icon_name(&self) -> String {
        "photo-spool".into()
    }

    fn tool_tip(&self) -> ksni::ToolTip {
        ksni::ToolTip {
            title: "Photo Spool".into(),
            ..Default::default()
        }
    }

    /// Left click checks for new media without opening the window.
    fn activate(&mut self, _x: i32, _y: i32) {
        queue(&self.qt, |mut c| {
            c.as_mut().refresh_devices();
            if let Some(rt) = &c.rt {
                rt.notifier
                    .send("Photo Spool", "Checked for new photos.", None, true, 2500);
            }
        });
    }

    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        use ksni::menu::StandardItem;
        vec![
            StandardItem {
                label: "Open Photo Spool".into(),
                activate: Box::new(|t: &mut Self| queue(&t.qt, |c| c.show_requested())),
                ..Default::default()
            }
            .into(),
            ksni::MenuItem::Separator,
            StandardItem {
                label: "Quit".into(),
                activate: Box::new(|_| super::shim::quit()),
                ..Default::default()
            }
            .into(),
        ]
    }
}

/// A second launch connects here instead of starting its own instance.
fn spawn_instance_listener(qt: &Qt) {
    let Some(listener) = crate::INSTANCE_LISTENER.lock().unwrap().take() else {
        return;
    };
    let qt = qt.clone();
    std::thread::spawn(move || {
        for conn in listener.incoming() {
            drop(conn);
            queue(&qt, |c| c.show_requested());
        }
    });
}

/// Pings systemd from the GUI thread, so a wedged event loop trips the watchdog.
fn spawn_watchdog(qt: &Qt) {
    let Some(usec) = std::env::var("WATCHDOG_USEC")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
    else {
        return;
    };
    let interval = Duration::from_millis((usec / 1000 / 2).max(1000));
    let qt = qt.clone();
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(interval);
            if qt.queue(|_| crate::sd_notify("WATCHDOG=1")).is_err() {
                return;
            }
        }
    });
}
