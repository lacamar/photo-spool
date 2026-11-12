use super::*;
use crate::testutil::Isolated;
use std::sync::mpsc::Receiver;

const FAKE_DNGLAB: &str = r#"#!/bin/sh
for a; do in="$out"; out="$a"; done
mkdir -p "$out"
ls "$in" >> "$(dirname "$0")/calls.log"
echo "--" >> "$(dirname "$0")/calls.log"
for f in "$in"/*; do
  n=$(basename "$f"); s="${n%.*}"
  cp -p "$f" "$out/$s.dng"
  echo "Status: Converted '$f' => '$out/$s.dng'"
done
"#;

struct Env {
    iso: Isolated,
    conn: Connection,
    card: PathBuf,
    dnglab: PathBuf,
}

impl Env {
    fn new() -> Self {
        let iso = Isolated::new();
        let conn = db::connect().unwrap();
        let card = iso.root.join("card");
        std::fs::create_dir_all(&card).unwrap();
        let bin = iso.root.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let dnglab = bin.join("dnglab");
        std::fs::write(&dnglab, FAKE_DNGLAB).unwrap();
        std::fs::set_permissions(&dnglab, std::os::unix::fs::PermissionsExt::from_mode(0o755))
            .unwrap();
        Self {
            iso,
            conn,
            card,
            dnglab,
        }
    }

    fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let p = self.card.join(name);
        std::fs::write(&p, bytes).unwrap();
        p
    }

    fn run_with(&self, req: ImportRequest, dnglab: Option<PathBuf>) -> (i64, Vec<Event>) {
        let (tx, rx) = mpsc::channel();
        let tx = Mutex::new(tx);
        let p = Pipeline::for_test(move |e| tx.lock().unwrap().send(e).unwrap(), dnglab);
        p.run_session(&self.conn, &req);
        drop(p);
        let events: Vec<Event> = Receiver::iter(&rx).collect();
        let id = self
            .conn
            .query_row("SELECT MAX(id) FROM sessions", [], |r| r.get(0))
            .unwrap();
        (id, events)
    }

    fn run(&self, selected: Option<&[&str]>, mark_only: bool) -> i64 {
        let req = ImportRequest {
            source_root: lossy(&self.card),
            device_label: "Test".into(),
            kind: "blockdev".into(),
            selected_filenames: selected.map(|s| s.iter().map(|x| x.to_string()).collect()),
            mark_only,
        };
        self.run_with(req, Some(self.dnglab.clone())).0
    }

    fn counts(&self, id: i64) -> (String, i64, i64, i64) {
        self.conn
            .query_row(
                "SELECT status, imported_count, duplicate_count, failed_count FROM sessions WHERE id = ?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .unwrap()
    }

    fn file_rows(&self, id: i64) -> Vec<(String, String, String)> {
        let mut stmt = self
            .conn
            .prepare("SELECT source_filename, status, dest_path FROM session_files WHERE session_id = ? ORDER BY sort_order")
            .unwrap();
        stmt.query_map([id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .unwrap()
            .map(Result::unwrap)
            .collect()
    }

    fn dnglab_calls(&self) -> Vec<Vec<String>> {
        let log =
            std::fs::read_to_string(self.dnglab.with_file_name("calls.log")).unwrap_or_default();
        log.split("--\n")
            .filter(|c| !c.is_empty())
            .map(|c| c.lines().map(String::from).collect())
            .collect()
    }

    fn staging_entries(&self) -> Vec<PathBuf> {
        std::fs::read_dir(paths::staging_dir())
            .map(|d| d.flatten().map(|e| e.path()).collect())
            .unwrap_or_default()
    }
}

#[test]
fn video_is_placed_with_correct_content() {
    let env = Env::new();
    let f = env.write("clip.mov", &b"fake video bytes".repeat(10000));
    let id = env.run(None, false);
    assert_eq!(env.counts(id), ("completed".into(), 1, 0, 0));
    let rows = env.file_rows(id);
    let dest = PathBuf::from(&rows[0].2);
    assert_eq!(util::suffix(&dest), ".mov");
    assert!(dest.starts_with(env.iso.root.join("pictures")));
    assert_eq!(std::fs::read(dest).unwrap(), std::fs::read(&f).unwrap());
    assert!(f.is_file(), "source must never be touched");
    assert!(
        env.dnglab_calls().is_empty(),
        "passthrough never reaches dnglab"
    );
}

#[test]
fn reimporting_the_same_video_is_a_duplicate() {
    let env = Env::new();
    env.write("clip.mov", &b"fake video bytes".repeat(10000));
    let first = env.run(None, false);
    assert_eq!(env.counts(first).1, 1);
    let second = env.run(None, false);
    assert_eq!(env.counts(second), ("completed".into(), 0, 1, 0));
}

#[test]
fn staging_dirs_are_cleaned_up_after_the_session() {
    let env = Env::new();
    env.write("clip.mov", &b"fake video bytes".repeat(10000));
    env.run(None, false);
    assert!(env.staging_entries().is_empty());
}

#[test]
fn staging_in_only_ever_contains_conversion_candidates() {
    let env = Env::new();
    env.write("DSC00001.ARW", &b"fake raw bytes".repeat(10000));
    env.write("clip.mov", &b"fake video bytes".repeat(10000));
    let id = env.run(None, false);
    let calls = env.dnglab_calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].len(), 1);
    assert!(calls[0][0].ends_with(".ARW"));
    assert_eq!(env.counts(id), ("completed".into(), 2, 0, 0));
}

#[test]
fn converted_file_is_placed_as_dng_and_progress_names_the_source() {
    let env = Env::new();
    env.write("DSC00001.ARW", &b"fake raw bytes".repeat(10000));
    let req = ImportRequest {
        source_root: lossy(&env.card),
        device_label: "Test".into(),
        kind: "blockdev".into(),
        ..Default::default()
    };
    let (id, events) = env.run_with(req, Some(env.dnglab.clone()));
    let rows = env.file_rows(id);
    assert_eq!(rows[0].1, "imported");
    assert!(rows[0].2.ends_with("_00001.dng"), "{}", rows[0].2);
    let phases: Vec<(&str, i64, i64, &str)> = events
        .iter()
        .filter_map(|e| match e {
            Event::Progress {
                phase,
                done,
                total,
                file,
                ..
            } => Some((*phase, *done, *total, file.as_str())),
            _ => None,
        })
        .collect();
    assert_eq!(
        phases,
        [
            (PHASE_SCANNING, 1, 1, ""),
            (PHASE_CHECKING, 1, 1, "DSC00001.ARW"),
            (PHASE_CONVERTING, 0, 1, ""),
            (PHASE_CONVERTING, 1, 1, "DSC00001.ARW"),
            (PHASE_PLACING, 1, 1, "DSC00001.ARW"),
        ]
    );
    assert_eq!(events.first(), Some(&Event::Started(id)));
    assert_eq!(events.last(), Some(&Event::Finished(id)));
}

#[test]
fn missing_dnglab_fails_the_session() {
    let env = Env::new();
    env.write("DSC00001.ARW", b"raw");
    let req = ImportRequest {
        source_root: lossy(&env.card),
        device_label: "T".into(),
        kind: "mtp".into(),
        ..Default::default()
    };
    let (id, events) = env.run_with(req, None);
    let (status, err): (String, String) = env
        .conn
        .query_row(
            "SELECT status, error_message FROM sessions WHERE id = ?",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(status, "failed");
    assert!(err.starts_with("The DNG converter (dnglab) isn't installed"));
    assert!(events.contains(&Event::DnglabUnavailable(id)));
}

#[test]
fn failed_conversion_records_dnglab_output() {
    let env = Env::new();
    std::fs::write(
        &env.dnglab,
        "#!/bin/sh\necho 'unsupported camera'\nexit 1\n",
    )
    .unwrap();
    env.write("DSC00001.ARW", b"raw");
    let id = env.run(None, false);
    assert_eq!(env.counts(id), ("completed".into(), 0, 0, 1));
    let err: String = env
        .conn
        .query_row(
            "SELECT error_message FROM session_files WHERE session_id = ?",
            [id],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(err, "unsupported camera");
}

#[test]
fn empty_card_completes_with_nothing() {
    let env = Env::new();
    let id = env.run(None, false);
    let (status, found, ejectable): (String, i64, String) = env
        .conn
        .query_row(
            "SELECT status, found_count, ejectable_path FROM sessions WHERE id = ?",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(
        (status.as_str(), found, ejectable),
        ("completed", 0, lossy(&env.card))
    );
}

#[test]
fn mark_only_records_ledger_without_placing_or_staging() {
    let env = Env::new();
    let f = env.write("clip.mov", &b"fake video bytes".repeat(10000));
    let id = env.run(Some(&["clip.mov"]), true);
    let mark_only: bool = env
        .conn
        .query_row("SELECT mark_only FROM sessions WHERE id = ?", [id], |r| {
            r.get(0)
        })
        .unwrap();
    assert!(mark_only);
    assert_eq!(env.counts(id).1, 1);
    let dest: String = env
        .conn
        .query_row(
            "SELECT dest_path FROM imports WHERE source_filename = 'clip.mov'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(dest, "");
    assert!(f.is_file());
    assert!(env.staging_entries().is_empty());
    assert!(!env.iso.root.join("pictures").exists());
}

#[test]
fn duplicate_content_in_one_batch_is_a_duplicate_pointing_at_the_placed_file() {
    let env = Env::new();
    let content = b"identical video bytes".repeat(10000);
    env.write("a.mov", &content);
    env.write("b.mov", &content);
    let id = env.run(None, false);
    assert_eq!(env.counts(id), ("completed".into(), 1, 1, 0));
    let rows = env.file_rows(id);
    assert_eq!(
        (rows[0].0.as_str(), rows[0].1.as_str()),
        ("a.mov", "imported")
    );
    assert_eq!(
        (rows[1].0.as_str(), rows[1].1.as_str()),
        ("b.mov", "duplicate")
    );
    assert!(!rows[0].2.is_empty());
    assert_eq!(rows[0].2, rows[1].2);
    let n: i64 = env
        .conn
        .query_row("SELECT COUNT(*) FROM imports", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 1);
}

#[test]
fn duplicate_content_raw_is_converted_once() {
    let env = Env::new();
    let content = b"identical raw bytes".repeat(10000);
    env.write("DSC00001.ARW", &content);
    env.write("DSC00002.ARW", &content);
    let id = env.run(None, false);
    assert_eq!(env.dnglab_calls()[0].len(), 1);
    assert_eq!(env.counts(id), ("completed".into(), 1, 1, 0));
}

#[test]
fn mark_only_duplicate_content_does_not_crash() {
    let env = Env::new();
    let content = b"identical video bytes".repeat(10000);
    env.write("a.mov", &content);
    env.write("b.mov", &content);
    let id = env.run(None, true);
    assert_eq!(env.counts(id), ("completed".into(), 1, 1, 0));
}

#[test]
fn only_selected_files_are_processed() {
    let env = Env::new();
    env.write("a.mov", &[b'a'; 1000]);
    env.write("b.mov", &[b'b'; 1000]);
    let id = env.run(Some(&["a.mov"]), false);
    let found: i64 = env
        .conn
        .query_row("SELECT found_count FROM sessions WHERE id = ?", [id], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(found, 2);
    assert_eq!(env.counts(id).1, 1);
}

#[test]
fn selecting_a_subset_still_infers_camera_model_from_the_full_card() {
    let env = Env::new();
    let still = env.write("DSC00001.ARW", &[b'a'; 1000]);
    let video = env.write("clip.mov", &[b'b'; 1000]);
    for (p, model) in [(&still, "ILCE-7RM3"), (&video, "")] {
        env.conn
            .execute(
                "INSERT INTO metadata_cache (path, size_bytes, camera_model, cached_at) VALUES (?, 1000, ?, 't')",
                params![lossy(p), model],
            )
            .unwrap();
    }
    env.run(Some(&["clip.mov"]), false);
    let model: String = env
        .conn
        .query_row(
            "SELECT camera_model FROM imports WHERE source_filename = 'clip.mov'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(model, "ILCE-7RM3");
}

#[test]
fn delete_originals_setting_removes_source_after_import() {
    let env = Env::new();
    db::set_setting(
        &env.conn,
        "delete_originals_after_import",
        &serde_json::json!(true),
    )
    .unwrap();
    let f = env.write("clip.mov", b"bytes");
    let id = env.run(None, false);
    assert_eq!(env.counts(id).1, 1);
    assert!(!f.exists());
}

#[test]
fn worker_thread_pauses_and_stops() {
    let env = Env::new();
    env.write("clip.mov", b"bytes");
    let (tx, rx) = mpsc::channel();
    let tx = Mutex::new(tx);
    let mut w = ImportWorker::start(move |e| {
        let _ = tx.lock().unwrap().send(e);
    });
    w.set_paused(true);
    w.submit(ImportRequest {
        source_root: lossy(&env.card),
        device_label: "T".into(),
        kind: "manual".into(),
        mark_only: true,
        ..Default::default()
    });
    assert!(
        rx.recv_timeout(Duration::from_millis(300)).is_err(),
        "paused worker must not start a session"
    );
    w.set_paused(false);
    assert!(matches!(
        rx.recv_timeout(Duration::from_secs(5)),
        Ok(Event::Started(_))
    ));
    w.stop();
}

#[test]
fn file_vanishing_mid_scan_is_skipped_not_failed() {
    let env = Env::new();
    let transient = env.write("IMG_0001D.DNG", b"transient");
    env.write("IMG_0002.mov", b"kept");
    // exiftool wrapper deletes the transient file first, as iOS does mid-scan
    let wrapper = env.dnglab.with_file_name("exiftool");
    let real = util::which("exiftool").unwrap();
    std::fs::write(
        &wrapper,
        format!(
            "#!/bin/sh\nrm -f '{}'\nexec '{}' \"$@\"\n",
            lossy(&transient),
            lossy(&real)
        ),
    )
    .unwrap();
    std::fs::set_permissions(
        &wrapper,
        std::os::unix::fs::PermissionsExt::from_mode(0o755),
    )
    .unwrap();
    let old_path = std::env::var("PATH").unwrap();
    // SAFETY: Isolated holds the env lock for this test
    unsafe {
        std::env::set_var(
            "PATH",
            format!("{}:{old_path}", lossy(wrapper.parent().unwrap())),
        )
    };
    let id = env.run(None, false);
    unsafe { std::env::set_var("PATH", old_path) };
    assert_eq!(env.counts(id), ("completed".into(), 1, 0, 0));
    assert_eq!(env.file_rows(id).len(), 1);
}

#[test]
fn session_error_marks_failed_and_still_finishes() {
    let env = Env::new();
    env.write("clip.mov", b"bytes");
    // staging dir path occupied by a file: creating the session's staging dirs fails
    std::fs::write(paths::staging_dir(), b"").unwrap();
    let req = ImportRequest {
        source_root: lossy(&env.card),
        device_label: "T".into(),
        kind: "manual".into(),
        ..Default::default()
    };
    let (id, events) = env.run_with(req, Some(env.dnglab.clone()));
    let (status, err): (String, String) = env
        .conn
        .query_row(
            "SELECT status, error_message FROM sessions WHERE id = ?",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(status, "failed");
    assert!(!err.is_empty());
    assert_eq!(events.last(), Some(&Event::Finished(id)));
}

#[test]
fn stop_cancels_running_session() {
    let env = Env::new();
    env.write("clip.mov", b"bytes");
    let p = Pipeline::for_test(|_| {}, Some(env.dnglab.clone()));
    p.pause.state.lock().unwrap().stopping = true;
    let req = ImportRequest {
        source_root: lossy(&env.card),
        device_label: "T".into(),
        kind: "manual".into(),
        ..Default::default()
    };
    p.run_session(&env.conn, &req);
    let status: String = env
        .conn
        .query_row("SELECT status FROM sessions", [], |r| r.get(0))
        .unwrap();
    assert_eq!(status, "cancelled");
    assert!(env.staging_entries().is_empty());
}

fn run_with_card_dropped(env: &Env, back_after: Option<Duration>) -> i64 {
    let card = env.card.clone();
    let (tx, _rx) = mpsc::channel::<Event>();
    let tx = Mutex::new(tx);
    let dropped = Mutex::new(false);
    let p = Pipeline::for_test(
        move |e| {
            if let Event::Progress {
                phase: PHASE_CHECKING,
                ..
            } = e
                && !std::mem::replace(&mut *dropped.lock().unwrap(), true)
            {
                let gone = card.with_extension("gone");
                std::fs::rename(&card, &gone).unwrap();
                if let Some(d) = back_after {
                    let card = card.clone();
                    std::thread::spawn(move || {
                        std::thread::sleep(d);
                        std::fs::rename(&gone, &card).unwrap();
                    });
                }
            }
            let _ = tx.lock().unwrap().send(e);
        },
        Some(env.dnglab.clone()),
    );
    let req = ImportRequest {
        source_root: lossy(&env.card),
        device_label: "Test".into(),
        kind: "mtp".into(),
        selected_filenames: None,
        mark_only: false,
    };
    p.run_session(&env.conn, &req);
    env.conn
        .query_row("SELECT MAX(id) FROM sessions", [], |r| r.get(0))
        .unwrap()
}

#[test]
fn source_dropping_mid_import_is_retried_once_remounted() {
    let env = Env::new();
    env.write("IMG_0001.mov", b"one");
    env.write("IMG_0002.mov", b"two");
    let id = run_with_card_dropped(&env, Some(Duration::from_millis(200)));
    assert_eq!(env.counts(id), ("completed".into(), 2, 0, 0));
}

#[test]
fn source_that_never_returns_fails_its_files() {
    let env = Env::new();
    env.write("IMG_0001.mov", b"one");
    env.write("IMG_0002.mov", b"two");
    let id = run_with_card_dropped(&env, None);
    assert_eq!(env.counts(id), ("completed".into(), 0, 0, 2));
}

#[test]
fn staging_dirs_are_cleaned_up_when_nothing_is_new() {
    let env = Env::new();
    env.write("clip.mov", b"seen before");
    env.run(None, false);
    env.run(None, false);
    assert!(env.staging_entries().is_empty());
}
