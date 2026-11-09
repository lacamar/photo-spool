use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub struct Captured {
    pub status: ExitStatus,
    pub stdout: Vec<u8>,
}

/// Runs `cmd` with stdout captured and stderr discarded; kills it after `timeout`.
pub fn run_capture(cmd: &mut Command, timeout: Duration) -> Option<Captured> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut out = child.stdout.take()?;
    let reader = thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = out.read_to_end(&mut buf);
        buf
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let stdout = reader.join().ok()?;
    Some(Captured { status, stdout })
}

pub fn spawn_detached(cmd: &mut Command) -> std::io::Result<()> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    thread::spawn(move || child.wait());
    Ok(())
}

pub fn which(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH")?
        .to_str()?
        .split(':')
        .map(|dir| Path::new(dir).join(name))
        .find(|p| p.is_file() && is_executable(p))
}

fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    p.metadata()
        .is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
}

pub fn lossy(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

pub fn file_name(p: &Path) -> String {
    p.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Python's `Path.suffix`: "" for dotfiles and names without an extension.
pub fn suffix(p: &Path) -> String {
    p.extension()
        .map(|e| format!(".{}", e.to_string_lossy()))
        .unwrap_or_default()
}

pub fn stem(p: &Path) -> String {
    p.file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// rename, falling back to copy + delete across filesystems (like shutil.move).
pub fn move_file(src: &Path, dest: &Path) -> std::io::Result<()> {
    match std::fs::rename(src, dest) {
        Err(e) if e.raw_os_error() == Some(libc::EXDEV) => {
            std::fs::copy(src, dest)?;
            let meta = src.metadata()?;
            let times = std::fs::FileTimes::new()
                .set_accessed(meta.accessed()?)
                .set_modified(meta.modified()?);
            std::fs::File::options()
                .write(true)
                .open(dest)?
                .set_times(times)?;
            std::fs::remove_file(src)
        }
        r => r,
    }
}

pub fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%'
            && i + 2 < b.len()
            && let Some(v) = std::str::from_utf8(&b[i + 1..i + 3])
                .ok()
                .and_then(|h| u8::from_str_radix(h, 16).ok())
        {
            out.push(v);
            i += 3;
            continue;
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percent_decode_matches_encode_uri_component() {
        assert_eq!(
            percent_decode("%2Fhome%2Fa%20b%2F%C3%A6.dng"),
            "/home/a b/æ.dng"
        );
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(percent_decode("%zz"), "%zz");
    }

    #[test]
    fn suffix_and_stem_follow_pathlib() {
        assert_eq!(suffix(Path::new("/a/DSC1.ARW")), ".ARW");
        assert_eq!(suffix(Path::new("/a/.hidden")), "");
        assert_eq!(stem(Path::new("/a/IMG_7731D.DNG")), "IMG_7731D");
    }

    #[test]
    fn run_capture_times_out() {
        assert!(run_capture(Command::new("sleep").arg("5"), Duration::from_millis(100)).is_none());
        let out = run_capture(Command::new("echo").arg("hi"), Duration::from_secs(5)).unwrap();
        assert_eq!(out.stdout, b"hi\n");
    }
}
