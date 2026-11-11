//! Session-bus integrations: notifications, suspend inhibit, file manager, XDG settings portal.
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use zbus::blocking::{Connection, Proxy};
use zbus::zvariant::{OwnedObjectPath, OwnedValue, Value};

const NOTIFY: (&str, &str, &str) = (
    "org.freedesktop.Notifications",
    "/org/freedesktop/Notifications",
    "org.freedesktop.Notifications",
);
const PORTAL: (&str, &str) = (
    "org.freedesktop.portal.Desktop",
    "/org/freedesktop/portal/desktop",
);
const APPEARANCE_NS: &str = "org.freedesktop.appearance";
pub const PREFER_DARK: u32 = 1;

/// Short timeout: a hung xdg-desktop-portal must never stall startup (no reply = no READY=1).
fn session() -> Option<Connection> {
    zbus::blocking::connection::Builder::session()
        .and_then(|b| b.method_timeout(std::time::Duration::from_secs(3)).build())
        .map_err(|e| log::warn!("no session bus: {e}"))
        .ok()
}

/// Unwraps nested variants (`v` inside `v`).
fn inner<'a>(v: &'a Value<'a>) -> &'a Value<'a> {
    match v {
        Value::Value(b) => inner(b),
        v => v,
    }
}

pub struct Notifier {
    conn: Option<Connection>,
    session_by_id: Arc<Mutex<HashMap<u32, i64>>>,
}

impl Notifier {
    /// `on_open(session_id)` runs on a listener thread when a notification's "Open" action fires.
    pub fn new(on_open: impl Fn(i64) + Send + 'static) -> Self {
        let conn = session();
        let session_by_id: Arc<Mutex<HashMap<u32, i64>>> = Arc::default();
        if let Some(c) = conn.clone() {
            let map = session_by_id.clone();
            std::thread::spawn(move || {
                let Ok(proxy) = Proxy::new(&c, NOTIFY.0, NOTIFY.1, NOTIFY.2) else {
                    return;
                };
                let (Ok(actions), Ok(closed)) = (
                    proxy.receive_signal("ActionInvoked"),
                    proxy.receive_signal("NotificationClosed"),
                ) else {
                    return;
                };
                let map2 = map.clone();
                std::thread::spawn(move || {
                    for msg in closed {
                        if let Ok((id, _reason)) = msg.body().deserialize::<(u32, u32)>() {
                            map2.lock().unwrap().remove(&id);
                        }
                    }
                });
                for msg in actions {
                    let Ok((id, key)) = msg.body().deserialize::<(u32, String)>() else {
                        continue;
                    };
                    if key != "open" {
                        continue;
                    }
                    let session = map.lock().unwrap().get(&id).copied();
                    if let Some(s) = session {
                        on_open(s);
                    }
                }
            });
        }
        Self {
            conn,
            session_by_id,
        }
    }

    /// Fire-and-forget; `transient` keeps it out of the daemon's history. Returns 0 on failure.
    pub fn send(
        &self,
        summary: &str,
        body: &str,
        session_id: Option<i64>,
        transient: bool,
        expire_ms: i32,
    ) -> u32 {
        let Some(conn) = &self.conn else { return 0 };
        let actions: Vec<&str> = if session_id.is_some() {
            vec!["open", "Open"]
        } else {
            vec![]
        };
        let mut hints: HashMap<&str, Value> = HashMap::new();
        if transient {
            hints.insert("transient", Value::Bool(true));
        }
        let reply = conn.call_method(
            Some(NOTIFY.0),
            NOTIFY.1,
            Some(NOTIFY.2),
            "Notify",
            &(
                "Photo Spool",
                0u32,
                "",
                summary,
                body,
                actions,
                hints,
                expire_ms,
            ),
        );
        let id = match reply.and_then(|m| m.body().deserialize::<u32>()) {
            Ok(id) => id,
            Err(e) => {
                log::warn!("Failed to send desktop notification: {e}");
                return 0;
            }
        };
        if let Some(s) = session_id {
            self.session_by_id.lock().unwrap().insert(id, s);
        }
        id
    }
}

/// Reference-counted suspend+idle inhibit for in-flight imports; best-effort.
#[derive(Default)]
pub struct Inhibitor {
    count: u32,
    /// The portal drops an inhibit when its sender disconnects: the connection lives as long as it.
    request: Option<(Connection, OwnedObjectPath)>,
}

impl Inhibitor {
    pub fn begin(&mut self) {
        self.count += 1;
        if self.count > 1 || self.request.is_some() {
            return;
        }
        const SUSPEND_AND_IDLE: u32 = 4 | 8;
        let options = HashMap::from([("reason", Value::from("Importing photos"))]);
        let result = session().ok_or(zbus::Error::Unsupported).and_then(|c| {
            let path = c
                .call_method(
                    Some(PORTAL.0),
                    PORTAL.1,
                    Some("org.freedesktop.portal.Inhibit"),
                    "Inhibit",
                    &("", SUSPEND_AND_IDLE, options),
                )?
                .body()
                .deserialize::<OwnedObjectPath>()?;
            Ok((c, path))
        });
        match result {
            Ok(p) => self.request = Some(p),
            Err(e) => log::info!("Could not inhibit idle/suspend via portal: {e}"),
        }
    }

    pub fn end(&mut self) {
        self.count = self.count.saturating_sub(1);
        if self.count > 0 {
            return;
        }
        let Some((c, path)) = self.request.take() else {
            return;
        };
        if let Err(e) = c.call_method(
            Some(PORTAL.0),
            path.as_str(),
            Some("org.freedesktop.portal.Request"),
            "Close",
            &(),
        ) {
            log::info!("Could not release idle/suspend inhibit: {e}");
        }
    }
}

/// Selects `file_uri` in the file manager via org.freedesktop.FileManager1.
pub fn show_in_file_manager(file_uri: &str) -> bool {
    session().is_some_and(|c| {
        c.call_method(
            Some("org.freedesktop.FileManager1"),
            "/org/freedesktop/FileManager1",
            Some("org.freedesktop.FileManager1"),
            "ShowItems",
            &(vec![file_uri], ""),
        )
        .is_ok()
    })
}

pub enum PortalChange {
    ColorScheme(u32),
    Accent(String),
}

fn accent_name(v: &Value) -> String {
    let Value::Structure(s) = inner(v) else {
        return String::new();
    };
    let rgb: Vec<f64> = s
        .fields()
        .iter()
        .filter_map(|f| f64::try_from(f).ok())
        .collect();
    let [r, g, b] = rgb[..] else {
        return String::new();
    };
    if ![r, g, b].iter().all(|c| (0.0..=1.0).contains(c)) {
        return String::new();
    }
    // QColor::fromRgbF(...).name(): 16-bit channels, top byte shown
    let ch = |c: f64| ((c * 65535.0).round() as u32) >> 8;
    format!("#{:02x}{:02x}{:02x}", ch(r), ch(g), ch(b))
}

pub struct Portal {
    conn: Option<Connection>,
}

impl Portal {
    pub fn new(on_change: impl Fn(PortalChange) + Send + 'static) -> Self {
        let conn = session();
        if let Some(c) = conn.clone() {
            std::thread::spawn(move || {
                let Ok(proxy) =
                    Proxy::new(&c, PORTAL.0, PORTAL.1, "org.freedesktop.portal.Settings")
                else {
                    return;
                };
                let Ok(signals) = proxy.receive_signal("SettingChanged") else {
                    return;
                };
                for msg in signals {
                    let Ok((ns, key, value)) =
                        msg.body().deserialize::<(String, String, OwnedValue)>()
                    else {
                        log::warn!("Malformed SettingChanged payload");
                        continue;
                    };
                    if ns != APPEARANCE_NS {
                        continue;
                    }
                    match key.as_str() {
                        "color-scheme" => {
                            if let Ok(v) = u32::try_from(inner(&value)) {
                                on_change(PortalChange::ColorScheme(v));
                            }
                        }
                        "accent-color" => on_change(PortalChange::Accent(accent_name(&value))),
                        _ => {}
                    }
                }
            });
        }
        Self { conn }
    }

    fn read(&self, key: &str) -> Option<OwnedValue> {
        self.conn
            .as_ref()?
            .call_method(
                Some(PORTAL.0),
                PORTAL.1,
                Some("org.freedesktop.portal.Settings"),
                "ReadOne",
                &(APPEARANCE_NS, key),
            )
            .ok()?
            .body()
            .deserialize::<OwnedValue>()
            .ok()
    }

    pub fn color_scheme(&self) -> u32 {
        self.read("color-scheme")
            .and_then(|v| u32::try_from(inner(&v)).ok())
            .unwrap_or(0)
    }

    pub fn accent(&self) -> String {
        self.read("accent-color")
            .map(|v| accent_name(&v))
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use zbus::zvariant::StructureBuilder;

    #[test]
    fn accent_matches_qcolor_name() {
        let s = StructureBuilder::new()
            .add_field(1.0f64)
            .add_field(0.5f64)
            .add_field(0.0f64)
            .build()
            .unwrap();
        assert_eq!(accent_name(&Value::Structure(s)), "#ff8000");
        let s = StructureBuilder::new()
            .add_field(0.2f64)
            .add_field(0.4f64)
            .add_field(0.6f64)
            .build()
            .unwrap();
        assert_eq!(accent_name(&Value::Structure(s)), "#336699");
        let bad = StructureBuilder::new()
            .add_field(2.0f64)
            .add_field(0.0f64)
            .add_field(0.0f64)
            .build()
            .unwrap();
        assert_eq!(accent_name(&Value::Structure(bad)), "");
        assert_eq!(accent_name(&Value::from(3u32)), "");
    }
}
