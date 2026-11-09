use std::env;
use std::path::PathBuf;

pub const APP_NAME: &str = "photo-spool";
const OLD_APP_NAME: &str = "photo-import";

fn home() -> PathBuf {
    env::var_os("HOME").map(PathBuf::from).unwrap_or_default()
}

fn xdg(var: &str, fallback: &[&str]) -> PathBuf {
    match env::var_os(var).filter(|v| !v.is_empty()) {
        Some(v) => PathBuf::from(v),
        None => fallback.iter().fold(home(), |p, c| p.join(c)),
    }
}

fn data_base() -> PathBuf {
    xdg("XDG_DATA_HOME", &[".local", "share"])
}

pub fn data_home() -> PathBuf {
    data_base().join(APP_NAME)
}

pub fn cache_home() -> PathBuf {
    xdg("XDG_CACHE_HOME", &[".cache"]).join(APP_NAME)
}

/// "v2" is the cache format version; bump it when the cached bytes change shape.
pub fn thumbnail_cache_dir() -> PathBuf {
    cache_home().join("thumbnails").join("v2")
}

pub fn theme_file() -> PathBuf {
    xdg("XDG_CONFIG_HOME", &[".config"])
        .join(APP_NAME)
        .join("theme.json")
}

pub fn db_path() -> PathBuf {
    data_home().join("data.db")
}

pub fn staging_dir() -> PathBuf {
    data_home().join("staging")
}

pub fn default_library_root() -> PathBuf {
    xdg("XDG_PICTURES_DIR", &["Pictures"])
}

pub fn runtime_dir() -> PathBuf {
    match env::var_os("XDG_RUNTIME_DIR").filter(|v| !v.is_empty()) {
        Some(v) => PathBuf::from(v),
        None => PathBuf::from(format!("/run/user/{}", unsafe { libc::getuid() })),
    }
}

fn migrate_data_home() {
    let new_home = data_home();
    if new_home.exists() {
        return;
    }
    let old_home = data_base().join(OLD_APP_NAME);
    if !old_home.exists() {
        return;
    }
    if let Some(parent) = new_home.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    // cross-filesystem override, permissions, ...: degrade to a fresh data dir
    let _ = std::fs::rename(old_home, new_home);
}

pub fn ensure_dirs() -> std::io::Result<()> {
    migrate_data_home();
    std::fs::create_dir_all(data_home())
}
