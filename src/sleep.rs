//! Releases the Wi-Fi iPhone mount before the system sleeps: a reader blocked on a dead FUSE mount
//! cannot be frozen, which stalls suspend. Holds a logind delay lock so the release finishes first.
use std::os::fd::OwnedFd;

use zbus::blocking::{Connection, Proxy};

use crate::wifi;

fn manager(conn: &Connection) -> zbus::Result<Proxy<'_>> {
    Proxy::new(
        conn,
        "org.freedesktop.login1",
        "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager",
    )
}

fn take_lock(m: &Proxy) -> Option<OwnedFd> {
    m.call::<_, _, zbus::zvariant::OwnedFd>(
        "Inhibit",
        &(
            "sleep",
            "Photo Spool",
            "Releasing the iPhone Wi-Fi mount",
            "delay",
        ),
    )
    .map(Into::into)
    .map_err(|e| log::warn!("Could not take sleep delay lock: {e}"))
    .ok()
}

fn on_prepare_for_sleep<L>(
    starting: bool,
    lock: &mut Option<L>,
    retake: impl FnOnce() -> Option<L>,
) {
    if starting {
        wifi::sleep_begin();
        *lock = None;
    } else {
        wifi::sleep_end();
        *lock = retake();
    }
}

pub fn spawn() {
    std::thread::spawn(|| {
        let run = || -> zbus::Result<()> {
            let conn = Connection::system()?;
            let m = manager(&conn)?;
            let signals = m.receive_signal("PrepareForSleep")?;
            let mut lock = take_lock(&m);
            for msg in signals {
                if let Ok(starting) = msg.body().deserialize::<bool>() {
                    on_prepare_for_sleep(starting, &mut lock, || take_lock(&m));
                }
            }
            Ok(())
        };
        if let Err(e) = run() {
            log::warn!("Sleep handling unavailable: {e}");
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_released_on_sleep_and_retaken_on_resume() {
        let _iso = crate::testutil::Isolated::new();
        let mut lock = Some(1);
        on_prepare_for_sleep(true, &mut lock, || Some(2));
        assert_eq!(lock, None);
        assert!(wifi::sleeping());
        on_prepare_for_sleep(false, &mut lock, || Some(2));
        assert_eq!(lock, Some(2));
        assert!(!wifi::sleeping());
    }
}
