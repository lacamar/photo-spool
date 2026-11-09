use std::collections::VecDeque;
use std::io::{self, BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use chrono::{DateTime, Datelike, Local, NaiveDate};

use crate::scanner::{self, Candidate};
use crate::util;

const EXIFTOOL_WRITE_TIMEOUT: Duration = Duration::from_secs(30);
const DNG_BACKWARD_VERSION: &str = "1.4.0.0";

/// (dng-preview, dng-thumbnail, full-size-preview) per preview_size setting.
fn preview_flags(size: &str) -> [&'static str; 3] {
    match size {
        "small" => ["false", "true", "false"],
        "full" => ["true", "true", "true"],
        _ => ["true", "true", "false"],
    }
}

fn converted_name(line: &str) -> Option<&str> {
    let rest = &line[line.find("Status: Converted '")? + "Status: Converted '".len()..];
    let end = rest.find('\'')?;
    (end > 0 && rest[end..].starts_with("' =>")).then(|| &rest[..end])
}

/// Runs dnglab over the staged batch, calling `on_progress(stem)` per converted file.
/// Returns (exit code, last 50 output lines).
pub fn convert_batch(
    dnglab: &Path,
    staging_in: &Path,
    staging_out: &Path,
    compression: &str,
    embed_raw: bool,
    preview_size: &str,
    mut on_progress: impl FnMut(&str),
) -> io::Result<(i32, String)> {
    std::fs::create_dir_all(staging_out)?;
    let [preview, thumbnail, full] = preview_flags(preview_size);
    let (reader, writer) = io::pipe()?;
    let mut child = Command::new(dnglab)
        .args([
            "convert",
            "-c",
            compression,
            "--embed-raw",
            if embed_raw { "true" } else { "false" },
        ])
        .args([
            "--dng-preview",
            preview,
            "--dng-thumbnail",
            thumbnail,
            "--full-size-preview",
            full,
        ])
        .args(["--keep-mtime", "true", "-r", "-f", "-v"])
        .arg(staging_in)
        .arg(staging_out)
        .stdin(Stdio::null())
        .stdout(writer.try_clone()?)
        .stderr(writer)
        .spawn()?;
    let mut tail: VecDeque<String> = VecDeque::new();
    for line in BufReader::new(reader).split(b'\n') {
        let Ok(line) = line else { break };
        let line = String::from_utf8_lossy(&line).into_owned() + "\n";
        if let Some(name) = converted_name(&line) {
            on_progress(&util::stem(Path::new(name)));
        }
        tail.push_back(line);
        if tail.len() > 50 {
            tail.pop_front();
        }
    }
    let status = child.wait()?;
    let code = status.code().unwrap_or(-1);
    Ok((code, tail.into_iter().collect()))
}

fn exiftool_write(path: &Path, args: &[String]) {
    let out = util::run_capture(
        Command::new("exiftool")
            .args(args)
            .args(["-overwrite_original", "-P"])
            .arg(path),
        EXIFTOOL_WRITE_TIMEOUT,
    );
    if !out.is_some_and(|o| o.status.success()) {
        log::warn!("exiftool {args:?} failed on {}", path.display());
    }
}

/// Non-fatal: the file is already placed either way.
pub fn set_dng_backward_version(path: &Path, original_raw_name: Option<&str>) {
    let mut args = vec![format!("-DNGBackwardVersion={DNG_BACKWARD_VERSION}")];
    if let Some(name) = original_raw_name.filter(|n| !n.is_empty()) {
        args.push(format!("-OriginalRawFileName={name}"));
    }
    exiftool_write(path, &args);
}

pub fn set_camera_model(path: &Path, model: &str) {
    exiftool_write(path, &[format!("-Model={model}")]);
}

fn sanitize(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_ascii_alphanumeric() || "._+-".contains(*c))
        .collect()
}

/// `suffix` is the placed file's extension: ".dng" for conversions, the original for passthrough.
pub fn library_dest_path(
    library_root: &Path,
    cand: &Candidate,
    suffix: &str,
) -> io::Result<PathBuf> {
    let captured = cand
        .captured_at
        .as_deref()
        .and_then(|s| s.get(..10))
        .and_then(|s| NaiveDate::parse_from_str(s, "%Y-%m-%d").ok());
    let d = match captured {
        Some(d) => d,
        // local calendar date, consistent with captured_at being camera-local
        None => DateTime::<Local>::from(cand.path.metadata()?.modified()?).date_naive(),
    };
    let model = sanitize(&cand.camera_model);
    let seq = scanner::shot_number(&util::file_name(&cand.path));
    let filename = format!("{}_{model}_{seq}{suffix}", d.format("%Y.%m.%d"));
    Ok(library_root
        .join(d.year().to_string())
        .join(d.format("%Y-%m").to_string())
        .join(d.format("%Y-%m-%d").to_string())
        .join(filename))
}

/// Appends " (n)" on collision. Check-then-use: safe only while imports run on one thread.
pub fn unique_dest_path(path: PathBuf) -> PathBuf {
    if !path.exists() {
        return path;
    }
    let (stem, suffix) = (util::stem(&path), util::suffix(&path));
    (2..)
        .map(|n| path.with_file_name(format!("{stem} ({n}){suffix}")))
        .find(|p| !p.exists())
        .unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::Isolated;

    fn cand(path: &Path, model: &str, captured: Option<&str>) -> Candidate {
        Candidate {
            path: path.into(),
            size_bytes: 1,
            camera_model: model.into(),
            captured_at: captured.map(Into::into),
            camera_model_inferred: false,
        }
    }

    #[test]
    fn dest_path_from_capture_date() {
        let c = cand(
            Path::new("/card/DSC01075.ARW"),
            "ILCE-7RM3",
            Some("2026-08-02T14:03:09"),
        );
        assert_eq!(
            library_dest_path(Path::new("/lib"), &c, ".dng").unwrap(),
            Path::new("/lib/2026/2026-08/2026-08-02/2026.08.02_ILCE-7RM3_01075.dng")
        );
    }

    #[test]
    fn dest_path_sanitizes_model() {
        let c = cand(
            Path::new("/card/IMG_0001.MOV"),
            "iPhone 15 Pro/Max",
            Some("2026-01-05T00:00:00"),
        );
        assert_eq!(
            library_dest_path(Path::new("/lib"), &c, ".mov").unwrap(),
            Path::new("/lib/2026/2026-01/2026-01-05/2026.01.05_iPhone15ProMax_0001.mov")
        );
    }

    #[test]
    fn dest_path_falls_back_to_local_mtime() {
        let iso = Isolated::new();
        let f = iso.root.join("clip.mp4");
        std::fs::write(&f, b"x").unwrap();
        let t = chrono::Local
            .with_ymd_and_hms(2025, 12, 31, 23, 30, 0)
            .unwrap();
        std::fs::File::options()
            .write(true)
            .open(&f)
            .unwrap()
            .set_modified(t.into())
            .unwrap();
        for captured in [None, Some("0000-00-00T00:00:00")] {
            let p = library_dest_path(Path::new("/lib"), &cand(&f, "", captured), ".mp4").unwrap();
            assert_eq!(
                p,
                Path::new("/lib/2025/2025-12/2025-12-31/2025.12.31__clip.mp4")
            );
        }
        assert!(
            library_dest_path(
                Path::new("/lib"),
                &cand(&iso.root.join("gone"), "", None),
                ".x"
            )
            .is_err()
        );
    }
    use chrono::TimeZone;

    #[test]
    fn unique_dest_appends_counter() {
        let iso = Isolated::new();
        let p = iso.root.join("a.dng");
        assert_eq!(unique_dest_path(p.clone()), p);
        std::fs::write(&p, b"").unwrap();
        std::fs::write(iso.root.join("a (2).dng"), b"").unwrap();
        assert_eq!(unique_dest_path(p), iso.root.join("a (3).dng"));
    }

    #[test]
    fn parses_dnglab_progress_lines() {
        assert_eq!(
            converted_name("Status: Converted '/s/in/abc.ARW' => '/s/out/abc.dng'"),
            Some("/s/in/abc.ARW")
        );
        assert_eq!(converted_name("Status: Failed '/s/in/abc.ARW'"), None);
    }

    #[test]
    fn convert_batch_streams_progress_and_tail() {
        let iso = Isolated::new();
        let script = iso.root.join("dnglab");
        std::fs::write(
            &script,
            "#!/bin/sh\nfor a; do :; done\necho \"Status: Converted '/in/h1.ARW' => '/out/h1.dng'\"\necho oops >&2\nexit 3\n",
        )
        .unwrap();
        std::fs::set_permissions(&script, std::os::unix::fs::PermissionsExt::from_mode(0o755))
            .unwrap();
        let mut seen = Vec::new();
        let (code, tail) = convert_batch(
            &script,
            &iso.root,
            &iso.root.join("out"),
            "lossless",
            false,
            "medium",
            |s| seen.push(s.to_string()),
        )
        .unwrap();
        assert_eq!(code, 3);
        assert_eq!(seen, ["h1"]);
        assert!(tail.ends_with("oops\n"));
        assert!(
            convert_batch(
                &iso.root.join("nope"),
                &iso.root,
                &iso.root.join("out"),
                "x",
                true,
                "full",
                |_| {}
            )
            .is_err()
        );
    }
}
