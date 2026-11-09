mod converter;
mod db;
mod desktop;
mod devices;
mod import;
mod paths;
mod qt;
mod scanner;
#[cfg(test)]
mod testutil;
mod thumbs;
mod util;
mod wifi;
mod workers;

use std::io::Write;
use std::os::linux::net::SocketAddrExt;
use std::os::unix::net::{UnixDatagram, UnixListener, UnixStream};
use std::sync::{Mutex, OnceLock};

use cxx_qt_lib::QQmlApplicationEngine;
use signal_hook::consts::{SIGINT, SIGTERM};

#[derive(Default)]
pub struct Args {
    pub demo: bool,
    /// Tray only on launch (systemd user service).
    pub start_hidden: bool,
}

static ARGS: OnceLock<Args> = OnceLock::new();
/// Taken by the controller once the UI is up.
pub static INSTANCE_LISTENER: Mutex<Option<UnixListener>> = Mutex::new(None);

pub fn args() -> &'static Args {
    ARGS.get_or_init(Args::default)
}

const USAGE: &str = "usage: photo-spool [-h] [--demo] [--start-hidden]";

fn parse_args() -> Args {
    let mut args = Args::default();
    for a in std::env::args().skip(1) {
        match a.as_str() {
            "--demo" => args.demo = true,
            "--start-hidden" => args.start_hidden = true,
            "-h" | "--help" => {
                println!(
                    "{USAGE}\n\n  --demo          Seed demo session history if the database is empty\n  --start-hidden  Don't show the window on launch -- just the tray icon"
                );
                std::process::exit(0);
            }
            other => {
                eprintln!("{USAGE}\nphoto-spool: error: unrecognized arguments: {other}");
                std::process::exit(2);
            }
        }
    }
    args
}

/// sd_notify(3); a no-op outside systemd.
pub fn sd_notify(state: &str) {
    let Some(addr) = std::env::var_os("NOTIFY_SOCKET") else {
        return;
    };
    let Ok(sock) = UnixDatagram::unbound() else {
        return;
    };
    let bytes = addr.as_encoded_bytes();
    let _ = match bytes.strip_prefix(b"@") {
        Some(name) => std::os::unix::net::SocketAddr::from_abstract_name(name)
            .and_then(|a| sock.send_to_addr(state.as_bytes(), &a)),
        None => sock.send_to(state.as_bytes(), &addr),
    };
}

/// Two instances would race to auto-import the same card: a second launch just raises the first.
/// Err(()) when another instance is running.
fn acquire_single_instance() -> Result<Option<UnixListener>, ()> {
    // same path QLocalServer used, so an old instance still answers during an upgrade
    let path = std::env::temp_dir().join("photo-spool-single-instance");
    if let Ok(mut s) = UnixStream::connect(&path) {
        let _ = s.write_all(b"show");
        return Err(());
    }
    let _ = std::fs::remove_file(&path);
    Ok(UnixListener::bind(&path)
        .map_err(|e| log::warn!("single-instance socket: {e}"))
        .ok())
}

struct StderrLog;

impl log::Log for StderrLog {
    fn enabled(&self, m: &log::Metadata) -> bool {
        m.level() <= log::Level::Warn
    }
    fn log(&self, r: &log::Record) {
        if self.enabled(r.metadata()) {
            eprintln!("{}:{}: {}", r.level(), r.target(), r.args());
        }
    }
    fn flush(&self) {}
}

fn main() {
    let _ = log::set_logger(&StderrLog).map(|_| log::set_max_level(log::LevelFilter::Warn));
    let _ = ARGS.set(parse_args());
    for (k, v) in [
        ("QT_QPA_PLATFORM", "wayland"),
        ("QT_QUICK_CONTROLS_STYLE", "Basic"),
    ] {
        if std::env::var_os(k).is_none() {
            // SAFETY: single-threaded at this point
            unsafe { std::env::set_var(k, v) };
        }
    }

    let Ok(listener) = acquire_single_instance() else {
        return;
    };
    *INSTANCE_LISTENER.lock().unwrap() = listener;

    qt::shim::create_app();
    let mut engine = QQmlApplicationEngine::new();
    if !engine.as_mut().is_some_and(qt::shim::load_main) {
        drop(engine);
        qt::shim::destroy_app();
        std::process::exit(-1);
    }
    sd_notify("READY=1");

    // route SIGINT/SIGTERM through quit() so workers shut down cleanly
    if let Ok(mut signals) = signal_hook::iterator::Signals::new([SIGINT, SIGTERM]) {
        std::thread::spawn(move || {
            for _ in signals.forever() {
                qt::shim::quit();
            }
        });
    }

    let code = qt::shim::exec();
    drop(engine);
    qt::shim::destroy_app();
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    #[test]
    fn version_matches_spec() {
        let spec = include_str!("../packaging/photo-spool.spec");
        let v = spec
            .lines()
            .find_map(|l| l.strip_prefix("Version:"))
            .unwrap()
            .trim();
        assert_eq!(v, env!("CARGO_PKG_VERSION"));
    }
}
