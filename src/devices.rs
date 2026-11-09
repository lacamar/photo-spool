//! Live registry of attached media: UDisks2 block devices and gvfs MTP/AFC mounts (plus Wi-Fi iPhones).
//! Polled, not signal-driven: UDisks2's InterfacesAdded payloads are deeply nested and a 2.5s poll
//! of GetManagedObjects is simple and reliable. Every source type degrades quietly when unavailable.
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use zbus::blocking::Connection;
use zbus::zvariant::{OwnedObjectPath, OwnedValue, Value};

use crate::{paths, util, wifi};

pub const POLL_INTERVAL: Duration = Duration::from_millis(2500);
const MTP_MOUNT_RETRY: Duration = Duration::from_secs(12);
const GVFS_FUSE_CHECK_RETRY: Duration = Duration::from_secs(10);
const UDISKS: &str = "org.freedesktop.UDisks2";
const FS_IFACE: &str = "org.freedesktop.UDisks2.Filesystem";
const BLOCK_IFACE: &str = "org.freedesktop.UDisks2.Block";
const DRIVE_IFACE: &str = "org.freedesktop.UDisks2.Drive";
// cameras in mass-storage mode expose tiny firmware partitions next to the card
const MIN_VOLUME_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq)]
pub enum DeviceEvent {
    /// Once per insertion, only for sources holding camera media: drives auto-import.
    SourceFound {
        root: String,
        label: String,
        kind: String,
    },
    SourceAdded {
        key: String,
        label: String,
        kind: String,
        mounted: bool,
        root: String,
    },
    SourceRemoved(String),
    MountChanged {
        key: String,
        mounted: bool,
        root: String,
    },
}

struct Source {
    label: String,
    kind: &'static str,
    mounted: bool,
    root: Option<String>,
    camera_media: Option<bool>,
}

/// Shallow DCIM check (also true when `root` itself is DCIM, as for narrowed iPhone roots).
fn find_dcim(root: &Path, depth: u32) -> bool {
    if util::file_name(root).to_uppercase() == "DCIM" && root.is_dir() {
        return true;
    }
    let Ok(entries) = std::fs::read_dir(root) else {
        return false;
    };
    let dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect();
    dirs.iter()
        .any(|d| util::file_name(d).to_uppercase() == "DCIM")
        || (depth > 0 && dirs.iter().any(|d| find_dcim(d, depth - 1)))
}

/// iPhone AFC roots also expose PhotoData/Mutations (semi-virtual, not originals): scope to DCIM.
/// iPhone-only: Sony keeps video in PRIVATE/M4ROOT beside DCIM.
fn narrow_iphone_root(root: &str) -> String {
    let dcim = Path::new(root).join("DCIM");
    if dcim.is_dir() {
        util::lossy(&dcim)
    } else {
        root.to_string()
    }
}

fn gvfs_dir() -> PathBuf {
    paths::runtime_dir().join("gvfs")
}

fn gvfs_fuse_mounted(dir: &Path) -> bool {
    let target = util::lossy(dir);
    std::fs::read_to_string("/proc/mounts").is_ok_and(|m| {
        m.lines().any(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            f.len() > 2 && f[1] == target && f[2] == "fuse.gvfsd-fuse"
        })
    })
}

/// Bare compositor sessions (niri) never start gvfsd-fuse, so MTP/AFC mounts never appear as paths.
fn ensure_gvfs_fuse_running(dir: &Path) {
    let _ = std::fs::create_dir_all(dir);
    if gvfs_fuse_mounted(dir) {
        return;
    }
    let bin = util::which("gvfsd-fuse").unwrap_or_else(|| "/usr/libexec/gvfsd-fuse".into());
    if !bin.is_file() {
        return;
    }
    let mut cmd = Command::new(bin);
    cmd.arg(dir);
    std::os::unix::process::CommandExt::process_group(&mut cmd, 0);
    if let Err(e) = util::spawn_detached(&mut cmd) {
        log::warn!("Could not start gvfsd-fuse: {e}");
    }
}

fn gvfs_fallback_label(dirname: &str) -> String {
    for prefix in ["mtp:host=", "afc:host="] {
        if let Some(host) = dirname.strip_prefix(prefix) {
            let s = host.split('_').next().unwrap_or("").replace("%20", " ");
            return if s.is_empty() { "Device".into() } else { s };
        }
    }
    dirname.to_string()
}

fn gio_display_name(path: &Path) -> String {
    let out = util::run_capture(
        Command::new("gio")
            .args(["info", "-a", "standard::display-name"])
            .arg(path)
            .env("LC_ALL", "C"),
        Duration::from_secs(5),
    );
    let Some(out) = out.filter(|o| o.status.success()) else {
        return String::new();
    };
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .find_map(|l| {
            l.strip_prefix("display name:")
                .map(|v| v.trim().to_string())
        })
        .unwrap_or_default()
}

fn run_lines(cmd: &mut Command) -> Option<String> {
    util::run_capture(cmd, Duration::from_secs(5))
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
}

fn list_connected_iphone_udids() -> HashSet<String> {
    run_lines(Command::new("idevice_id").arg("-l"))
        .map(|s| {
            s.lines()
                .map(str::trim)
                .filter(|l| !l.is_empty())
                .map(String::from)
                .collect()
        })
        .unwrap_or_default()
}

fn parse_unmounted_gvfs_uris(text: &str) -> HashSet<String> {
    let mut uris = HashSet::new();
    let mut relevant = false;
    for line in text.lines().map(str::trim) {
        if line.starts_with("Volume(") || line.starts_with("Mount(") {
            relevant = false;
        } else if line.starts_with("Type:") {
            relevant = line.contains("MTP") || line.contains("Afc") || line.contains("AFC");
        } else if relevant && let Some(uri) = line.strip_prefix("activation_root=") {
            uris.insert(uri.trim().to_string());
        }
    }
    uris
}

fn gio_mount(uri: &str) {
    let _ = util::spawn_detached(Command::new("gio").args(["mount", uri]));
}

type Ifaces = HashMap<String, HashMap<String, OwnedValue>>;

fn prop<'a>(ifaces: Option<&'a Ifaces>, iface: &str, name: &str) -> Option<&'a Value<'a>> {
    ifaces?.get(iface)?.get(name).map(|v| &**v)
}

fn prop_str(ifaces: Option<&Ifaces>, iface: &str, name: &str) -> String {
    match prop(ifaces, iface, name) {
        Some(Value::Str(s)) => s.to_string(),
        _ => String::new(),
    }
}

fn prop_bool(ifaces: Option<&Ifaces>, iface: &str, name: &str) -> bool {
    matches!(prop(ifaces, iface, name), Some(Value::Bool(true)))
}

fn mount_points(ifaces: &Ifaces) -> Vec<String> {
    let Some(Value::Array(points)) = prop(Some(ifaces), FS_IFACE, "MountPoints") else {
        return vec![];
    };
    points
        .iter()
        .filter_map(|p| match p {
            Value::Array(bytes) => {
                let raw: Vec<u8> = bytes.iter().filter_map(|b| u8::try_from(b).ok()).collect();
                let end = raw.iter().rposition(|b| *b != 0).map_or(0, |i| i + 1);
                Some(String::from_utf8_lossy(&raw[..end]).into_owned())
            }
            _ => None,
        })
        .collect()
}

pub struct DeviceWatcher {
    emit: Box<dyn Fn(DeviceEvent) + Send>,
    mtp_enabled: bool,
    sources: HashMap<String, Source>,
    auto_mount_attempted: HashSet<String>,
    auto_import_fired: HashSet<String>,
    mtp_mount_attempts: HashMap<String, Instant>,
    gvfs_fuse_last_check: Option<Instant>,
    gvfs_unmounted_list_last_check: Option<Instant>,
    usb_iphones: HashSet<String>,
    wifi: Option<wifi::WifiIphones>,
    bus: Option<Connection>,
}

fn due(last: Option<Instant>, every: Duration) -> bool {
    last.is_none_or(|t| t.elapsed() >= every)
}

impl DeviceWatcher {
    pub fn new(mtp_enabled: bool, emit: impl Fn(DeviceEvent) + Send + 'static) -> Self {
        let bus = zbus::blocking::connection::Builder::system()
            .and_then(|b| b.method_timeout(Duration::from_secs(25)).build())
            .map_err(|e| log::warn!("Could not connect to system bus for UDisks2: {e}"))
            .ok();
        Self {
            emit: Box::new(emit),
            mtp_enabled,
            sources: HashMap::new(),
            auto_mount_attempted: HashSet::new(),
            auto_import_fired: HashSet::new(),
            mtp_mount_attempts: HashMap::new(),
            gvfs_fuse_last_check: None,
            gvfs_unmounted_list_last_check: None,
            usb_iphones: HashSet::new(),
            wifi: Some(wifi::WifiIphones::new()),
            bus,
        }
    }

    pub fn set_mtp_enabled(&mut self, enabled: bool) {
        self.mtp_enabled = enabled;
    }

    pub fn stop(&mut self) {
        if let Some(w) = self.wifi.take() {
            w.close();
        }
    }

    pub fn poll(&mut self) {
        if let Err(e) = self.poll_blockdevs() {
            log::warn!("UDisks2 poll failed: {e}");
        }
        if self.mtp_enabled {
            self.poll_gvfs_devices();
        }
    }

    fn poll_blockdevs(&mut self) -> zbus::Result<()> {
        let Some(bus) = self.bus.clone() else {
            return Ok(());
        };
        let objects: HashMap<OwnedObjectPath, Ifaces> = bus
            .call_method(
                Some(UDISKS),
                "/org/freedesktop/UDisks2",
                Some("org.freedesktop.DBus.ObjectManager"),
                "GetManagedObjects",
                &(),
            )?
            .body()
            .deserialize()?;

        let mut current = HashSet::new();
        let mut keys: Vec<&OwnedObjectPath> = objects.keys().collect();
        keys.sort_by(|a, b| a.as_str().cmp(b.as_str()));
        for obj_path in keys {
            let ifaces = &objects[obj_path];
            let key = obj_path.to_string();
            if !ifaces.contains_key(FS_IFACE) {
                continue;
            }
            let drive = match prop(Some(ifaces), BLOCK_IFACE, "Drive") {
                Some(Value::ObjectPath(p)) if p.as_str() != "/" => Some(p.to_string()),
                _ => None,
            };
            let drive_ifaces = drive.as_deref().and_then(|d| {
                objects
                    .iter()
                    .find(|(k, _)| k.as_str() == d)
                    .map(|(_, v)| v)
            });
            let removable = drive.is_some()
                && (prop_bool(drive_ifaces, DRIVE_IFACE, "Removable")
                    || prop_bool(drive_ifaces, DRIVE_IFACE, "MediaRemovable"));
            if !removable {
                continue;
            }
            let size = match prop(Some(ifaces), BLOCK_IFACE, "Size") {
                Some(Value::U64(s)) => *s,
                _ => 0,
            };
            if size > 0 && size < MIN_VOLUME_BYTES {
                continue;
            }
            current.insert(key.clone());

            let mut root = mount_points(ifaces).into_iter().next();
            let drive_label = prop_str(drive_ifaces, DRIVE_IFACE, "Model");
            let mut label = if !drive_label.is_empty() {
                drive_label
            } else if let Some(r) = &root {
                util::file_name(Path::new(r))
            } else {
                key.rsplit('/').next().unwrap_or("").to_string()
            };
            if label.is_empty() {
                label = "Removable media".into();
            }
            let fs_label = prop_str(Some(ifaces), BLOCK_IFACE, "IdLabel");
            if !fs_label.is_empty() && fs_label != label {
                label = format!("{label} · {fs_label}");
            }

            if let Some(existing) = self.sources.get_mut(&key) {
                if existing.mounted != root.is_some() || existing.root != root {
                    existing.mounted = root.is_some();
                    existing.root = root.clone();
                    (self.emit)(DeviceEvent::MountChanged {
                        key: key.clone(),
                        mounted: root.is_some(),
                        root: root.clone().unwrap_or_default(),
                    });
                }
                if let Some(r) = root.filter(|_| existing.camera_media.is_none()) {
                    self.maybe_fire_auto_import(&key, &r, &label, "blockdev");
                }
                continue;
            }
            self.sources.insert(
                key.clone(),
                Source {
                    label: label.clone(),
                    kind: "blockdev",
                    mounted: root.is_some(),
                    root: root.clone(),
                    camera_media: None,
                },
            );
            (self.emit)(DeviceEvent::SourceAdded {
                key: key.clone(),
                label: label.clone(),
                kind: "blockdev".into(),
                mounted: root.is_some(),
                root: root.clone().unwrap_or_default(),
            });
            if root.is_none() && self.auto_mount_attempted.insert(key.clone()) {
                root = self.mount_blockdev(&key);
                if let Some(r) = &root {
                    let s = self.sources.get_mut(&key).unwrap();
                    s.mounted = true;
                    s.root = Some(r.clone());
                    (self.emit)(DeviceEvent::MountChanged {
                        key: key.clone(),
                        mounted: true,
                        root: r.clone(),
                    });
                }
            }
            if let Some(r) = root {
                self.maybe_fire_auto_import(&key, &r, &label, "blockdev");
            }
        }

        let gone: Vec<String> = self
            .sources
            .iter()
            .filter(|(k, s)| s.kind == "blockdev" && !current.contains(*k))
            .map(|(k, _)| k.clone())
            .collect();
        for key in gone {
            self.sources.remove(&key);
            self.auto_mount_attempted.remove(&key);
            self.auto_import_fired.remove(&key);
            (self.emit)(DeviceEvent::SourceRemoved(key));
        }
        Ok(())
    }

    fn mount_blockdev(&self, obj_path: &str) -> Option<String> {
        let opts: HashMap<&str, Value> = HashMap::new();
        let result = self
            .bus
            .as_ref()?
            .call_method(Some(UDISKS), obj_path, Some(FS_IFACE), "Mount", &(opts,))
            .and_then(|m| m.body().deserialize::<String>());
        result
            .map_err(|e| log::info!("Could not mount {obj_path}: {e}"))
            .ok()
    }

    /// On-demand mount of an unmounted card (blockdev only).
    pub fn mount_source(&mut self, key: &str) -> Option<String> {
        let source = self.sources.get(key)?;
        if source.kind != "blockdev" || source.mounted {
            return source.root.clone();
        }
        let root = self.mount_blockdev(key)?;
        let label = {
            let s = self.sources.get_mut(key).unwrap();
            s.mounted = true;
            s.root = Some(root.clone());
            s.label.clone()
        };
        (self.emit)(DeviceEvent::MountChanged {
            key: key.into(),
            mounted: true,
            root: root.clone(),
        });
        self.maybe_fire_auto_import(key, &root, &label, "blockdev");
        Some(root)
    }

    fn maybe_fire_auto_import(&mut self, key: &str, root: &str, label: &str, kind: &str) {
        let has_dcim = find_dcim(Path::new(root), 2);
        if let Some(s) = self.sources.get_mut(key) {
            s.camera_media = Some(has_dcim);
        }
        if has_dcim && self.auto_import_fired.insert(key.to_string()) {
            (self.emit)(DeviceEvent::SourceFound {
                root: root.into(),
                label: label.into(),
                kind: kind.into(),
            });
        }
    }

    fn poll_gvfs_devices(&mut self) {
        let gvfs = gvfs_dir();
        if due(self.gvfs_fuse_last_check, GVFS_FUSE_CHECK_RETRY) {
            self.gvfs_fuse_last_check = Some(Instant::now());
            ensure_gvfs_fuse_running(&gvfs);
        }
        // ",port=" AFC dirs are per-app document shares, never the camera roll
        let mut gvfs_dirs: Vec<String> = std::fs::read_dir(&gvfs)
            .map(|d| {
                d.flatten()
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .filter(|n| {
                        (n.starts_with("mtp:host=") || n.starts_with("afc:host="))
                            && !(n.starts_with("afc:host=") && n.contains(",port="))
                    })
                    .collect()
            })
            .unwrap_or_default();
        gvfs_dirs.sort();

        let mut current: HashSet<String> =
            gvfs_dirs.iter().map(|n| format!("gvfs-dir:{n}")).collect();
        let usb = self.usb_iphones.clone();
        let wifi_mounts = self.wifi.as_mut().map(|w| w.poll(&usb)).unwrap_or_default();
        for (udid, mountpoint) in wifi_mounts {
            let key = format!("wifi:{udid}");
            current.insert(key.clone());
            if !self.sources.contains_key(&key) {
                let root = narrow_iphone_root(&util::lossy(&mountpoint));
                let label = "iPhone (Wi-Fi)".to_string();
                self.sources.insert(
                    key.clone(),
                    Source {
                        label: label.clone(),
                        kind: "iphone",
                        mounted: true,
                        root: Some(root.clone()),
                        camera_media: None,
                    },
                );
                (self.emit)(DeviceEvent::SourceAdded {
                    key: key.clone(),
                    label: label.clone(),
                    kind: "iphone".into(),
                    mounted: true,
                    root: root.clone(),
                });
                self.maybe_fire_auto_import(&key, &root, &label, "mtp");
            }
        }
        for name in &gvfs_dirs {
            let key = format!("gvfs-dir:{name}");
            let mut root = util::lossy(&gvfs.join(name));
            let mut label = gio_display_name(Path::new(&root));
            if label.is_empty() {
                label = gvfs_fallback_label(name);
            }
            // "iphone" is display-only; sessions always record "mtp"
            let kind = if name.starts_with("afc:host=") {
                "iphone"
            } else {
                "mtp"
            };
            if kind == "iphone" {
                root = narrow_iphone_root(&root);
            }
            if let Some(s) = self.sources.get_mut(&key) {
                s.label = label.clone();
                if s.camera_media.is_none() {
                    self.maybe_fire_auto_import(&key, &root, &label, "mtp");
                }
                continue;
            }
            self.sources.insert(
                key.clone(),
                Source {
                    label: label.clone(),
                    kind,
                    mounted: true,
                    root: Some(root.clone()),
                    camera_media: None,
                },
            );
            (self.emit)(DeviceEvent::SourceAdded {
                key: key.clone(),
                label: label.clone(),
                kind: kind.into(),
                mounted: true,
                root: root.clone(),
            });
            self.maybe_fire_auto_import(&key, &root, &label, "mtp");
        }

        let gone: Vec<String> = self
            .sources
            .iter()
            .filter(|(k, s)| (s.kind == "mtp" || s.kind == "iphone") && !current.contains(*k))
            .map(|(k, _)| k.clone())
            .collect();
        for key in gone {
            self.sources.remove(&key);
            self.auto_import_fired.remove(&key);
            (self.emit)(DeviceEvent::SourceRemoved(key));
        }

        // `gio mount -li` / `idevice_id` fork processes: list at most once per retry window
        if !due(self.gvfs_unmounted_list_last_check, MTP_MOUNT_RETRY) {
            return;
        }
        self.gvfs_unmounted_list_last_check = Some(Instant::now());
        let unmounted = run_lines(Command::new("gio").args(["mount", "-li"]))
            .map(|s| parse_unmounted_gvfs_uris(&s))
            .unwrap_or_default();
        for uri in unmounted {
            if self.mount_attempt_due(&uri) {
                gio_mount(&uri);
            }
        }
        // an iPhone's main AFC share never shows up in `gio mount -li`: mount it by UDID
        self.usb_iphones = list_connected_iphone_udids();
        for udid in self.usb_iphones.clone() {
            if gvfs_dirs.contains(&format!("afc:host={udid}")) {
                continue;
            }
            if self.mount_attempt_due(&format!("afc-main:{udid}")) {
                gio_mount(&format!("afc://{udid}/"));
            }
        }
    }

    fn mount_attempt_due(&mut self, key: &str) -> bool {
        if !due(self.mtp_mount_attempts.get(key).copied(), MTP_MOUNT_RETRY) {
            return false;
        }
        self.mtp_mount_attempts.insert(key.into(), Instant::now());
        true
    }

    pub fn eject(&mut self, root_path: &str) -> bool {
        let key = self
            .sources
            .iter()
            .find(|(_, s)| s.root.as_deref() == Some(root_path))
            .map(|(k, _)| k.clone());
        if let Some(k) = &key {
            if self.sources[k].kind == "blockdev"
                && let Some(bus) = &self.bus
            {
                let opts: HashMap<&str, Value> = HashMap::new();
                return match bus.call_method(
                    Some(UDISKS),
                    k.as_str(),
                    Some(FS_IFACE),
                    "Unmount",
                    &(opts,),
                ) {
                    Ok(_) => {
                        let s = self.sources.get_mut(k).unwrap();
                        s.mounted = false;
                        s.root = None;
                        (self.emit)(DeviceEvent::MountChanged {
                            key: k.clone(),
                            mounted: false,
                            root: String::new(),
                        });
                        true
                    }
                    Err(e) => {
                        log::info!("Unmount via UDisks2 failed for {root_path}: {e}");
                        false
                    }
                };
            }
            if let (Some(udid), Some(w)) = (k.strip_prefix("wifi:"), self.wifi.as_mut()) {
                w.eject(udid);
                self.sources.remove(k);
                (self.emit)(DeviceEvent::SourceRemoved(k.clone()));
                return true;
            }
        }
        if util::run_capture(
            Command::new("gio").args(["mount", "-u", root_path]),
            Duration::from_secs(15),
        )
        .is_none()
        {
            return false;
        }
        if let Some(k) = key {
            self.sources.remove(&k);
            (self.emit)(DeviceEvent::SourceRemoved(k));
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::Isolated;

    #[test]
    fn dcim_detection() {
        let iso = Isolated::new();
        let card = iso.root.join("card");
        std::fs::create_dir_all(card.join("x/y/DCIM")).unwrap();
        assert!(find_dcim(&card, 2));
        assert!(!find_dcim(&card, 0));
        assert!(find_dcim(&card.join("x/y/DCIM"), 0));
        assert!(!find_dcim(&iso.root.join("missing"), 2));
    }

    #[test]
    fn iphone_root_narrows_to_dcim_only_when_present() {
        let iso = Isolated::new();
        let r = util::lossy(&iso.root);
        assert_eq!(narrow_iphone_root(&r), r);
        std::fs::create_dir(iso.root.join("DCIM")).unwrap();
        assert_eq!(narrow_iphone_root(&r), format!("{r}/DCIM"));
    }

    #[test]
    fn fallback_labels() {
        assert_eq!(
            gvfs_fallback_label("mtp:host=SONY%20Corp_ILCE_7RM3"),
            "SONY Corp"
        );
        assert_eq!(gvfs_fallback_label("afc:host=_x"), "Device");
        assert_eq!(gvfs_fallback_label("smb-share:x"), "smb-share:x");
    }

    #[test]
    fn parses_gio_mount_listing() {
        let text = "Volume(0): Card\n  Type: GProxyVolume (GProxyVolumeMonitorUDisks2)\n  activation_root=file:///x\n\
                    Volume(1): ILCE-7RM3\n  Type: GProxyVolume (GProxyVolumeMonitorMTP)\n  activation_root=mtp://Sony_ILCE/\n\
                    Mount(0): Phone\n  Type: GProxyMount (GProxyVolumeMonitorAfc)\n  activation_root=afc://abc/\n";
        assert_eq!(
            parse_unmounted_gvfs_uris(text),
            HashSet::from(["mtp://Sony_ILCE/".into(), "afc://abc/".into()])
        );
    }
}
