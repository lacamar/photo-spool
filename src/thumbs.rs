//! Thumbnail bytes for `image://thumb/<url-encoded-path>`: the embedded raw preview via exiftool, or
//! one ffmpeg frame for videos. Cached on disk keyed by (path, mtime, size); the C++ provider
//! shrinks fresh extractions before handing them to `store` (Qt does the image codec work).
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use sha2::{Digest, Sha256};

use crate::{paths, scanner, util};

const EXTRACT_TIMEOUT: Duration = Duration::from_secs(10);
const VIDEO_FRAME_TIMEOUT: Duration = Duration::from_secs(15);
// 0.5s skips an all-black first frame; 0 covers clips shorter than that
const VIDEO_FRAME_SEEKS: [&str; 2] = ["00:00:00.5", "00:00:00"];

fn cache_path(path: &str) -> Option<PathBuf> {
    let m = std::fs::metadata(path).ok()?;
    let mtime_ns = m.mtime() as i128 * 1_000_000_000 + m.mtime_nsec() as i128;
    let key: String = Sha256::digest(format!("{path}\n{mtime_ns}\n{}", m.size()))
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Some(paths::thumbnail_cache_dir().join(format!("{key}.jpg")))
}

fn extract_preview(path: &str) -> Option<Vec<u8>> {
    util::run_capture(
        Command::new("exiftool").args(["-b", "-PreviewImage", path]),
        EXTRACT_TIMEOUT,
    )
    .filter(|o| o.status.success() && !o.stdout.is_empty())
    .map(|o| o.stdout)
}

fn extract_video_frame(path: &str) -> Option<Vec<u8>> {
    for seek in VIDEO_FRAME_SEEKS {
        let out = util::run_capture(
            Command::new("ffmpeg").args([
                "-nostdin",
                "-ss",
                seek,
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-1",
                "-f",
                "mjpeg",
                "pipe:1",
            ]),
            VIDEO_FRAME_TIMEOUT,
        )?;
        if out.status.success() && !out.stdout.is_empty() {
            return Some(out.stdout);
        }
    }
    None
}

/// (bytes, true if served from cache). Empty bytes means no thumbnail.
pub fn load(id: &str) -> (Vec<u8>, bool) {
    let path = util::percent_decode(id);
    if let Some(bytes) = cache_path(&path).and_then(|c| std::fs::read(c).ok()) {
        return (bytes, true);
    }
    let raw = if scanner::is_video(Path::new(&path)) {
        extract_video_frame(&path)
    } else {
        extract_preview(&path)
    };
    (raw.unwrap_or_default(), false)
}

/// Write-then-rename so concurrent provider threads never read a partial entry.
pub fn store(id: &str, bytes: &[u8]) {
    let Some(cache) = cache_path(&util::percent_decode(id)) else {
        return;
    };
    let Some(dir) = cache.parent() else { return };
    let tmp = dir.join(format!(
        ".{}.{:?}.tmp",
        std::process::id(),
        std::thread::current().id()
    ));
    let result = std::fs::create_dir_all(dir)
        .and_then(|_| std::fs::write(&tmp, bytes))
        .and_then(|_| std::fs::rename(&tmp, &cache));
    if let Err(e) = result {
        let _ = std::fs::remove_file(&tmp);
        log::debug!(
            "Could not write thumbnail cache entry {}: {e}",
            cache.display()
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::Isolated;

    #[test]
    fn cache_roundtrip_and_invalidation() {
        let iso = Isolated::new();
        let f = iso.root.join("a b.dng");
        std::fs::write(&f, b"not an image").unwrap();
        let id = util::lossy(&f).replace(' ', "%20");
        assert_eq!(load(&id), (vec![], false));
        store(&id, b"jpeg");
        assert_eq!(load(&id), (b"jpeg".to_vec(), true));
        std::fs::write(&f, b"changed size!").unwrap();
        assert!(!load(&id).1);
        assert!(
            cache_path(&util::lossy(&f))
                .unwrap()
                .starts_with(iso.root.join("cache/photo-spool/thumbnails/v2"))
        );
    }

    #[test]
    fn video_frame_extraction() {
        let iso = Isolated::new();
        let clip = iso.root.join("clip.mp4");
        let made = Command::new("ffmpeg")
            .args([
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=32x32:d=0.2",
            ])
            .arg(&clip)
            .status()
            .is_ok_and(|s| s.success());
        if !made {
            return;
        }
        let (bytes, cached) = load(&util::lossy(&clip));
        assert!(!cached);
        assert_eq!(&bytes[..2], &[0xff, 0xd8]);
    }
}
