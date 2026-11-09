use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use chrono::{NaiveDate, NaiveTime};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::util::{self, lossy};

const EXIFTOOL_BATCH_TIMEOUT: Duration = Duration::from_secs(180);
const HASH_CHUNK_SIZE: usize = 1024 * 1024;

// dnglab's entire supported-format list plus DNG passthrough; undecodable ones fail per file
const RAW_EXTENSIONS: &[&str] = &[
    ".arw", ".srf", ".sr2", ".cr2", ".cr3", ".crw", ".crm", ".nef", ".nrw", ".raf", ".orf", ".ori",
    ".rw2", ".raw", ".rwl", ".pef", ".mrw", ".srw", ".erf", ".kdc", ".dcs", ".dcr", ".3fr", ".fff",
    ".mef", ".iiq", ".mos", ".ari", ".x3f", ".qtk", ".dng",
];
const VIDEO_EXTENSIONS: &[&str] = &[".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi"];

#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    pub path: PathBuf,
    pub size_bytes: i64,
    pub camera_model: String,
    /// Camera-local wall-clock time, ISO 8601.
    pub captured_at: Option<String>,
    /// camera_model was borrowed from sibling files; written back into placed videos.
    pub camera_model_inferred: bool,
}

fn lower_suffix(p: &Path) -> String {
    util::suffix(p).to_lowercase()
}

pub fn is_dng(p: &Path) -> bool {
    lower_suffix(p) == ".dng"
}

pub fn is_video(p: &Path) -> bool {
    VIDEO_EXTENSIONS.contains(&lower_suffix(p).as_str())
}

/// "IMG_7731D" -> Some("IMG_7731"): a deferred-processing variant of a plain-numbered shot.
fn deferred_plain_stem(stem: &str) -> Option<&str> {
    let plain = stem.trim_end_matches(|c: char| c.is_ascii_alphabetic());
    (plain.len() < stem.len() && plain.ends_with(|c: char| c.is_ascii_digit())).then_some(plain)
}

/// Drops "IMG_7731D.DNG" when "IMG_7731.DNG" sits in the same directory with the same extension.
fn drop_deferred_duplicates(files: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut stems: HashMap<(PathBuf, String), HashSet<String>> = HashMap::new();
    for f in &files {
        stems
            .entry((f.parent().unwrap_or(Path::new("")).into(), lower_suffix(f)))
            .or_default()
            .insert(util::stem(f));
    }
    files
        .into_iter()
        .filter(|f| {
            let stem = util::stem(f);
            let Some(plain) = deferred_plain_stem(&stem) else {
                return true;
            };
            !stems[&(f.parent().unwrap_or(Path::new("")).into(), lower_suffix(f))].contains(plain)
        })
        .collect()
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(ft) = entry.file_type() else { continue };
        if ft.is_dir() {
            walk(&path, out);
        } else if path.is_file() {
            out.push(path);
        }
    }
}

pub fn find_importable_files(root: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    walk(root, &mut files);
    files.retain(|p| {
        let ext = lower_suffix(p);
        (RAW_EXTENSIONS.contains(&ext.as_str()) || VIDEO_EXTENSIONS.contains(&ext.as_str()))
            && !util::file_name(p).starts_with('.')
    });
    files.sort();
    drop_deferred_duplicates(files)
}

/// Metadata per file: cached rows (revalidated by size) plus one exiftool batch for the rest.
pub fn read_metadata(files: &[PathBuf], conn: Option<&Connection>) -> HashMap<PathBuf, Candidate> {
    if files.is_empty() {
        return HashMap::new();
    }
    let mut combined = match conn {
        Some(c) => load_cached_metadata(c, files),
        None => HashMap::new(),
    };
    let to_fetch: Vec<PathBuf> = files
        .iter()
        .filter(|f| !combined.contains_key(*f))
        .cloned()
        .collect();
    let fresh = read_metadata_uncached(&to_fetch);
    if let Some(c) = conn
        && !fresh.is_empty()
        && let Err(e) = store_metadata_cache(c, &fresh)
    {
        log::warn!("metadata cache write failed: {e}");
    }
    combined.extend(fresh);
    fill_missing_camera_models(&mut combined);
    combined
}

fn load_cached_metadata(conn: &Connection, files: &[PathBuf]) -> HashMap<PathBuf, Candidate> {
    let mut out = HashMap::new();
    let Ok(mut stmt) = conn.prepare_cached(
        "SELECT size_bytes, camera_model, captured_at, camera_model_inferred FROM metadata_cache WHERE path = ?",
    ) else {
        return out;
    };
    for f in files {
        let row = stmt
            .query_row([lossy(f)], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, Option<String>>(2)?,
                    r.get::<_, bool>(3)?,
                ))
            })
            .optional();
        let Ok(Some((size, model, captured, inferred))) = row else {
            continue;
        };
        // mount paths get reused across reformatted cards: only trust a hit whose size still matches
        if f.metadata().map(|m| m.len() as i64).ok() != Some(size) {
            continue;
        }
        out.insert(
            f.clone(),
            Candidate {
                path: f.clone(),
                size_bytes: size,
                camera_model: model,
                captured_at: captured,
                camera_model_inferred: inferred,
            },
        );
    }
    out
}

fn store_metadata_cache(
    conn: &Connection,
    candidates: &HashMap<PathBuf, Candidate>,
) -> rusqlite::Result<()> {
    let now = chrono::Local::now()
        .naive_local()
        .format("%Y-%m-%dT%H:%M:%S%.6f")
        .to_string();
    let tx = conn.unchecked_transaction()?;
    {
        let mut stmt = tx.prepare(
            "INSERT INTO metadata_cache (path, size_bytes, camera_model, captured_at, camera_model_inferred, \
             cached_at) VALUES (?, ?, ?, ?, ?, ?) \
             ON CONFLICT(path) DO UPDATE SET size_bytes = excluded.size_bytes, \
             camera_model = excluded.camera_model, captured_at = excluded.captured_at, \
             camera_model_inferred = excluded.camera_model_inferred, cached_at = excluded.cached_at",
        )?;
        for (f, c) in candidates {
            stmt.execute(params![
                lossy(f),
                c.size_bytes,
                c.camera_model,
                c.captured_at,
                c.camera_model_inferred,
                now
            ])?;
        }
    }
    tx.commit()
}

fn read_metadata_uncached(files: &[PathBuf]) -> HashMap<PathBuf, Candidate> {
    if files.is_empty() {
        return HashMap::new();
    }
    // argfile keeps a full card under ARG_MAX
    let argfile = std::env::temp_dir().join(format!(
        "photo-spool-exif-{}-{:?}.txt",
        std::process::id(),
        std::thread::current().id()
    ));
    let list = files
        .iter()
        .map(|f| lossy(f))
        .collect::<Vec<_>>()
        .join("\n");
    if std::fs::write(&argfile, list).is_err() {
        return HashMap::new();
    }
    let result = util::run_capture(
        Command::new("exiftool").arg("-@").arg(&argfile).args([
            "-j",
            "-Model",
            "-DateTimeOriginal",
            "-CreateDate",
            "-FileSize#",
        ]),
        EXIFTOOL_BATCH_TIMEOUT,
    );
    let _ = std::fs::remove_file(&argfile);
    // exiftool exits 1 on any per-file warning but still emits JSON for the rest
    let Some(result) = result.filter(|r| !r.stdout.is_empty()) else {
        return HashMap::new();
    };
    let Ok(Value::Array(records)) = serde_json::from_slice(&result.stdout) else {
        return HashMap::new();
    };
    parse_exiftool_records(&records)
}

fn parse_exiftool_records(records: &[Value]) -> HashMap<PathBuf, Candidate> {
    let mut out = HashMap::new();
    for rec in records {
        let Some(source) = rec
            .get("SourceFile")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        else {
            continue;
        };
        let path = PathBuf::from(source);
        let size = match rec.get("FileSize") {
            Some(Value::Number(n)) => n.as_f64().map_or(0, |f| f as i64),
            Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
            _ => path.metadata().map_or(0, |m| m.len() as i64),
        };
        let model = match rec.get("Model") {
            Some(Value::String(s)) => s.trim().to_string(),
            Some(Value::Number(n)) => n.to_string(),
            Some(Value::Bool(true)) => "True".into(),
            _ => String::new(),
        };
        let captured_at = parse_exif_datetime(rec.get("DateTimeOriginal"))
            .or_else(|| parse_exif_datetime(rec.get("CreateDate")));
        out.insert(
            path.clone(),
            Candidate {
                path,
                size_bytes: size,
                camera_model: model,
                captured_at,
                camera_model_inferred: false,
            },
        );
    }
    out
}

/// If every file with a known model agrees on one, files missing it borrow that model.
fn fill_missing_camera_models(candidates: &mut HashMap<PathBuf, Candidate>) {
    let models: HashSet<&str> = candidates
        .values()
        .map(|c| c.camera_model.as_str())
        .filter(|m| !m.is_empty())
        .collect();
    if models.len() != 1 {
        return;
    }
    let fallback = models.into_iter().next().unwrap().to_string();
    for c in candidates
        .values_mut()
        .filter(|c| c.camera_model.is_empty())
    {
        c.camera_model = fallback.clone();
        c.camera_model_inferred = true;
    }
}

/// "YYYY:MM:DD HH:MM:SS..." -> "YYYY-MM-DDTHH:MM:SS"; None for placeholders like "0000:00:00 00:00:00".
pub fn parse_exif_datetime(value: Option<&Value>) -> Option<String> {
    let s = value?.as_str()?.as_bytes();
    let shape = b"dddd:dd:dd dd:dd:dd";
    if s.len() < shape.len()
        || !shape.iter().zip(s).all(|(k, c)| {
            if *k == b'd' {
                c.is_ascii_digit()
            } else {
                k == c
            }
        })
    {
        return None;
    }
    let num = |a: usize, b: usize| {
        std::str::from_utf8(&s[a..b])
            .unwrap()
            .parse::<u32>()
            .unwrap()
    };
    let (y, mo, d, h, mi, sec) = (
        num(0, 4),
        num(5, 7),
        num(8, 10),
        num(11, 13),
        num(14, 16),
        num(17, 19),
    );
    if y < 1 {
        return None;
    }
    NaiveDate::from_ymd_opt(y as i32, mo, d)?;
    NaiveTime::from_hms_opt(h, mi, sec)?;
    Some(format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{sec:02}"))
}

/// Trailing digit run plus optional letters of the stem ("DSC01075.ARW" -> "01075", "IMG_7731D" -> "7731D").
pub fn shot_number(filename: &str) -> String {
    let stem = util::stem(Path::new(filename));
    let without_letters = stem.trim_end_matches(|c: char| c.is_ascii_alphabetic());
    let digits_start = without_letters
        .trim_end_matches(|c: char| c.is_ascii_digit())
        .len();
    if digits_start == without_letters.len() {
        return stem;
    }
    stem[digits_start..].to_string()
}

fn hex(digest: impl AsRef<[u8]>) -> String {
    digest.as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

pub fn hash_file(path: &Path) -> io::Result<String> {
    let mut h = Sha256::new();
    let mut f = File::open(path)?;
    let mut buf = vec![0; HASH_CHUNK_SIZE];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex(h.finalize()))
}

/// Hashes `path` while copying it to `dest`, so a slow source is read exactly once.
pub fn hash_and_stage(path: &Path, dest: &Path) -> io::Result<String> {
    let result = (|| {
        let mut h = Sha256::new();
        let mut src = File::open(path)?;
        let mut out = File::create(dest)?;
        let mut buf = vec![0; HASH_CHUNK_SIZE];
        loop {
            let n = src.read(&mut buf)?;
            if n == 0 {
                break;
            }
            h.update(&buf[..n]);
            out.write_all(&buf[..n])?;
        }
        Ok(hex(h.finalize()))
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(dest);
    }
    result
}

/// Cheap (model, filename, size) pre-check; returns the existing dest_path.
pub fn quick_duplicate_match(
    conn: &Connection,
    model: &str,
    filename: &str,
    size: i64,
) -> rusqlite::Result<Option<String>> {
    conn.prepare_cached(
        "SELECT dest_path FROM imports WHERE camera_model = ? AND source_filename = ? AND source_bytes = ? LIMIT 1",
    )?
    .query_row(params![model, filename, size], |r| r.get(0))
    .optional()
}

pub fn hash_duplicate_check(
    conn: &Connection,
    source_hash: &str,
) -> rusqlite::Result<Option<String>> {
    conn.prepare_cached("SELECT dest_path FROM imports WHERE source_hash = ?")?
        .query_row([source_hash], |r| r.get(0))
        .optional()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db;
    use crate::testutil::Isolated;
    use serde_json::json;

    #[test]
    fn exif_datetime_parsing() {
        let p = |s: &str| parse_exif_datetime(Some(&json!(s)));
        assert_eq!(
            p("2026:08:02 14:03:09").as_deref(),
            Some("2026-08-02T14:03:09")
        );
        assert_eq!(
            p("2026:08:02 14:03:09+02:00").as_deref(),
            Some("2026-08-02T14:03:09")
        );
        assert_eq!(p("0000:00:00 00:00:00"), None);
        assert_eq!(p("2026:02:30 00:00:00"), None);
        assert_eq!(p("2026:01:01 24:00:00"), None);
        assert_eq!(p("2026-01-01 10:00:00"), None);
        assert_eq!(p(""), None);
        assert_eq!(parse_exif_datetime(Some(&json!(20260101))), None);
        assert_eq!(parse_exif_datetime(None), None);
    }

    #[test]
    fn shot_numbers() {
        assert_eq!(shot_number("DSC01075.ARW"), "01075");
        assert_eq!(shot_number("IMG_7731D.DNG"), "7731D");
        assert_eq!(shot_number("ab12cd34.nef"), "34");
        assert_eq!(shot_number("1a2b.raf"), "2b");
        assert_eq!(shot_number("clip.mov"), "clip");
        assert_eq!(shot_number("2022.06.03__221935228.mp4"), "221935228");
    }

    #[test]
    fn kinds() {
        assert!(is_dng(Path::new("x/IMG.DNG")));
        assert!(is_video(Path::new("x/C0001.MP4")));
        assert!(!is_video(Path::new("x/a.arw")));
    }

    fn touch(p: &Path, bytes: &[u8]) {
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, bytes).unwrap();
    }

    #[test]
    fn finds_importable_files_sorted_skipping_hidden_and_deferred() {
        let iso = Isolated::new();
        let r = iso.root.join("card");
        for n in [
            "DCIM/100/DSC2.ARW",
            "DCIM/100/DSC1.arw",
            "DCIM/100/.DSC3.ARW",
            "DCIM/100/notes.txt",
            "PRIVATE/M4ROOT/CLIP/C0001.MP4",
            "DCIM/101/IMG_7731.DNG",
            "DCIM/101/IMG_7731D.DNG",
            "DCIM/101/IMG_7732D.DNG",
            "DCIM/102/IMG_7731D.DNG",
        ] {
            touch(&r.join(n), b"x");
        }
        let found: Vec<String> = find_importable_files(&r)
            .iter()
            .map(|p| lossy(p.strip_prefix(&r).unwrap()))
            .collect();
        assert_eq!(
            found,
            [
                "DCIM/100/DSC1.arw",
                "DCIM/100/DSC2.ARW",
                "DCIM/101/IMG_7731.DNG",
                "DCIM/101/IMG_7732D.DNG",
                "DCIM/102/IMG_7731D.DNG",
                "PRIVATE/M4ROOT/CLIP/C0001.MP4",
            ]
        );
        assert!(find_importable_files(&iso.root.join("missing")).is_empty());
    }

    #[test]
    fn exiftool_records_and_model_inference() {
        let recs = json!([
            {"SourceFile": "/c/a.arw", "Model": " ILCE-7RM3 ", "DateTimeOriginal": "2026:08:02 10:00:00", "FileSize": 100},
            {"SourceFile": "/c/b.mp4", "CreateDate": "2026:08:02 11:00:00", "FileSize": 50},
            {"SourceFile": "/c/c.mp4", "CreateDate": "0000:00:00 00:00:00", "FileSize": 5},
            {"Model": "orphan"}
        ]);
        let mut m = parse_exiftool_records(recs.as_array().unwrap());
        assert_eq!(m.len(), 3);
        fill_missing_camera_models(&mut m);
        let b = &m[Path::new("/c/b.mp4")];
        assert_eq!(
            (b.camera_model.as_str(), b.camera_model_inferred),
            ("ILCE-7RM3", true)
        );
        assert_eq!(b.captured_at.as_deref(), Some("2026-08-02T11:00:00"));
        assert_eq!(m[Path::new("/c/c.mp4")].captured_at, None);
        assert!(!m[Path::new("/c/a.arw")].camera_model_inferred);
    }

    #[test]
    fn mixed_models_are_not_inferred() {
        let recs = json!([
            {"SourceFile": "/c/a.arw", "Model": "A"},
            {"SourceFile": "/c/b.arw", "Model": "B"},
            {"SourceFile": "/c/c.mp4"}
        ]);
        let mut m = parse_exiftool_records(recs.as_array().unwrap());
        fill_missing_camera_models(&mut m);
        assert_eq!(m[Path::new("/c/c.mp4")].camera_model, "");
    }

    #[test]
    fn metadata_cache_hit_requires_same_size() {
        let iso = Isolated::new();
        let conn = db::connect().unwrap();
        let f = iso.root.join("card/a.arw");
        touch(&f, b"12345");
        let cand = Candidate {
            path: f.clone(),
            size_bytes: 5,
            camera_model: "M".into(),
            captured_at: Some("2026-01-01T00:00:00".into()),
            camera_model_inferred: false,
        };
        store_metadata_cache(&conn, &HashMap::from([(f.clone(), cand.clone())])).unwrap();
        assert_eq!(
            load_cached_metadata(&conn, std::slice::from_ref(&f))[&f],
            cand
        );
        std::fs::write(&f, b"123456").unwrap();
        assert!(load_cached_metadata(&conn, std::slice::from_ref(&f)).is_empty());
    }

    #[test]
    fn hashing_and_staging() {
        let iso = Isolated::new();
        let f = iso.root.join("a.bin");
        touch(&f, b"hello");
        let expect = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";
        assert_eq!(hash_file(&f).unwrap(), expect);
        let dest = iso.root.join("staged");
        assert_eq!(hash_and_stage(&f, &dest).unwrap(), expect);
        assert_eq!(std::fs::read(&dest).unwrap(), b"hello");
        assert!(hash_and_stage(&iso.root.join("missing"), &dest).is_err());
        assert!(!dest.exists());
    }

    #[test]
    fn duplicate_lookups() {
        let _iso = Isolated::new();
        let conn = db::connect().unwrap();
        conn.execute(
            "INSERT INTO imports (source_hash, source_filename, source_bytes, camera_model, dest_path, dest_bytes, \
             imported_at) VALUES ('h1', 'DSC1.ARW', 10, 'M', '/lib/x.dng', 5, 't')",
            [],
        )
        .unwrap();
        assert_eq!(
            quick_duplicate_match(&conn, "M", "DSC1.ARW", 10)
                .unwrap()
                .as_deref(),
            Some("/lib/x.dng")
        );
        assert_eq!(
            quick_duplicate_match(&conn, "M", "DSC1.ARW", 11).unwrap(),
            None
        );
        assert_eq!(
            hash_duplicate_check(&conn, "h1").unwrap().as_deref(),
            Some("/lib/x.dng")
        );
        assert_eq!(hash_duplicate_check(&conn, "h2").unwrap(), None);
    }
}
