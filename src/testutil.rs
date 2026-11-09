use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard};

static ENV_LOCK: Mutex<()> = Mutex::new(());
const XDG_VARS: [(&str, &str); 4] = [
    ("XDG_DATA_HOME", "data"),
    ("XDG_CACHE_HOME", "cache"),
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_PICTURES_DIR", "pictures"),
];

/// Points every XDG var at a fresh temp dir for one test. Load-bearing: tests must never
/// touch the real ~/Pictures or data dir. Holds a global lock since env is process-wide.
pub struct Isolated {
    pub root: PathBuf,
    _guard: MutexGuard<'static, ()>,
}

impl Isolated {
    pub fn new() -> Self {
        let guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let root = std::env::temp_dir().join(format!(
            "photo-spool-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        for (var, sub) in XDG_VARS {
            // SAFETY: serialized by ENV_LOCK; tests touching env all hold it
            unsafe { std::env::set_var(var, root.join(sub)) };
        }
        Self {
            root,
            _guard: guard,
        }
    }
}

impl Drop for Isolated {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
