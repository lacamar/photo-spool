//! iPhones over Wi-Fi: mDNS discovery plus a minimal usbmuxd stand-in socket, so stock
//! `ifuse --network` can mount a paired phone read-only. The mDNS MAC -> UDID map is learned from
//! lockdown's BonjourFullServiceName while on USB, falling back to the pair record's WiFiMACAddress.
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::net::IpAddr;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use mdns_sd::{ServiceDaemon, ServiceEvent};
use plist::{Dictionary, Value};

use crate::{paths, util};

const SERVICE_TYPE: &str = "_apple-mobdev2._tcp.local.";
const LOCKDOWN_DIR: &str = "/var/lib/lockdown";
const MOUNT_RETRY: Duration = Duration::from_secs(15);

type Addr = (IpAddr, u32);

fn runtime_dir() -> PathBuf {
    paths::runtime_dir().join("photo-spool")
}

/// Native Linux sockaddr_in/_in6, which libimobiledevice copies verbatim.
pub fn sockaddr((ip, scope): Addr) -> Vec<u8> {
    match ip {
        IpAddr::V4(v4) => [
            &(libc::AF_INET as u16).to_ne_bytes()[..],
            &[0; 2],
            &v4.octets(),
            &[0; 8],
        ]
        .concat(),
        IpAddr::V6(v6) => [
            &(libc::AF_INET6 as u16).to_ne_bytes()[..],
            &[0; 6],
            &v6.octets(),
            &scope.to_ne_bytes(),
        ]
        .concat(),
    }
}

pub fn mac_of(service_name: &str) -> String {
    service_name.split('@').next().unwrap_or("").to_lowercase()
}

fn read_plist(path: &Path) -> Dictionary {
    plist::from_file::<_, Dictionary>(path).unwrap_or_default()
}

/// /proc/mounts, not a stat: stat on a dead network FUSE mount can block.
fn mounted(mp: &Path) -> bool {
    let target = util::lossy(mp);
    std::fs::read_to_string("/proc/self/mounts").is_ok_and(|m| {
        m.lines()
            .any(|l| l.split_whitespace().nth(1) == Some(target.as_str()))
    })
}

/// An ifuse mount whose AFC connection dropped (phone locked/asleep) stays in the mount table
/// but fails every access with EIO/ENOTCONN. A slow reply is not treated as dead.
fn dead(mp: &Path) -> bool {
    let (tx, rx) = std::sync::mpsc::channel();
    let path = mp.to_path_buf();
    std::thread::spawn(move || {
        let _ = tx.send(std::fs::read_dir(path).err());
    });
    matches!(rx.recv_timeout(Duration::from_secs(3)), Ok(Some(_)))
}

type Devices = Arc<Mutex<HashMap<String, Addr>>>;

struct Muxer {
    sock_path: PathBuf,
}

impl Muxer {
    fn start(sock_path: PathBuf, devices: Devices) -> std::io::Result<Self> {
        if let Some(p) = sock_path.parent() {
            std::fs::create_dir_all(p)?;
        }
        let _ = std::fs::remove_file(&sock_path);
        let listener = UnixListener::bind(&sock_path)?;
        std::thread::spawn(move || {
            for conn in listener.incoming().flatten() {
                let devices = devices.clone();
                std::thread::spawn(move || serve(conn, &devices));
            }
        });
        Ok(Self { sock_path })
    }

    fn close(&self) {
        let _ = std::fs::remove_file(&self.sock_path);
    }
}

fn device_list(devices: &Devices) -> Vec<Value> {
    let mut items: Vec<(String, Addr)> = devices
        .lock()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), *v))
        .collect();
    items.sort();
    items
        .into_iter()
        .enumerate()
        .map(|(i, (udid, addr))| {
            let id = Value::Integer((i as i64 + 1).into());
            let props = Dictionary::from_iter([
                (
                    "ConnectionType".to_string(),
                    Value::String("Network".into()),
                ),
                ("DeviceID".into(), id.clone()),
                ("SerialNumber".into(), Value::String(udid)),
                ("NetworkAddress".into(), Value::Data(sockaddr(addr))),
            ]);
            Value::Dictionary(Dictionary::from_iter([
                ("DeviceID".to_string(), id),
                ("MessageType".into(), Value::String("Attached".into())),
                ("Properties".into(), Value::Dictionary(props)),
            ]))
        })
        .collect()
}

fn dict(pairs: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
    Value::Dictionary(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn reply(req: &Dictionary, devices: &Devices) -> Vec<Value> {
    let result = |n: i64| {
        dict([
            ("MessageType", Value::String("Result".into())),
            ("Number", Value::Integer(n.into())),
        ])
    };
    match req.get("MessageType").and_then(Value::as_string) {
        Some("ListDevices") => {
            return vec![dict([("DeviceList", Value::Array(device_list(devices)))])];
        }
        Some("ReadPairRecord") => {
            let udid = req
                .get("PairRecordID")
                .and_then(Value::as_string)
                .unwrap_or("");
            let path = Path::new(LOCKDOWN_DIR).join(format!("{udid}.plist"));
            if !udid.contains('/')
                && path.is_file()
                && let Ok(data) = std::fs::read(&path)
            {
                return vec![dict([("PairRecordData", Value::Data(data))])];
            }
        }
        Some("ReadBUID") => {
            let conf = read_plist(&Path::new(LOCKDOWN_DIR).join("SystemConfiguration.plist"));
            if let Some(buid) = conf
                .get("SystemBUID")
                .and_then(Value::as_string)
                .filter(|s| !s.is_empty())
            {
                return vec![dict([("BUID", Value::String(buid.into()))])];
            }
        }
        Some("Listen") => return [vec![result(0)], device_list(devices)].concat(),
        _ => {}
    }
    vec![result(1)]
}

fn serve(mut conn: UnixStream, devices: &Devices) {
    loop {
        let mut header = [0u8; 16];
        if conn.read_exact(&mut header).is_err() {
            return;
        }
        let word = |i: usize| u32::from_le_bytes(header[i * 4..i * 4 + 4].try_into().unwrap());
        let (length, tag) = (word(0) as usize, word(3));
        let mut body = vec![0; length.saturating_sub(16)];
        if conn.read_exact(&mut body).is_err() {
            return;
        }
        let Ok(req) = plist::from_bytes::<Dictionary>(&body) else {
            return;
        };
        for (i, msg) in reply(&req, devices).into_iter().enumerate() {
            let mut out = Vec::new();
            if msg.to_writer_xml(&mut out).is_err() {
                return;
            }
            let header = [16 + out.len() as u32, 1, 8, if i == 0 { tag } else { 0 }];
            let frame: Vec<u8> = header
                .iter()
                .flat_map(|w| w.to_le_bytes())
                .chain(out)
                .collect();
            if conn.write_all(&frame).is_err() {
                return;
            }
        }
    }
}

/// Owned by the device watcher; `poll()` runs on its poll tick.
pub struct WifiIphones {
    found: Arc<Mutex<HashMap<String, Addr>>>,
    devices: Devices,
    mount_attempts: HashMap<String, Instant>,
    learned: HashSet<String>,
    ejected: HashSet<String>,
    map_file: PathBuf,
    daemon: Option<ServiceDaemon>,
    muxer: Option<Muxer>,
}

impl WifiIphones {
    pub fn new() -> Self {
        let mut me = Self {
            found: Arc::default(),
            devices: Arc::default(),
            mount_attempts: HashMap::new(),
            learned: HashSet::new(),
            ejected: HashSet::new(),
            map_file: paths::data_home().join("wifi-iphones.json"),
            daemon: None,
            muxer: None,
        };
        let started = Muxer::start(runtime_dir().join("usbmuxd.sock"), me.devices.clone())
            .map_err(|e| e.to_string())
            .and_then(|m| {
                me.muxer = Some(m);
                let daemon = ServiceDaemon::new().map_err(|e| e.to_string())?;
                let rx = daemon.browse(SERVICE_TYPE).map_err(|e| e.to_string())?;
                let found = me.found.clone();
                std::thread::spawn(move || {
                    while let Ok(ev) = rx.recv() {
                        match ev {
                            ServiceEvent::ServiceResolved(info) => {
                                let addrs: Vec<Addr> = info
                                    .addresses
                                    .iter()
                                    .map(|a| match a {
                                        mdns_sd::ScopedIp::V6(v6) => {
                                            (IpAddr::V6(*v6.addr()), v6.scope_id().index)
                                        }
                                        a => (a.to_ip_addr(), 0),
                                    })
                                    .collect();
                                let pick =
                                    addrs.iter().find(|(ip, _)| ip.is_ipv4()).or(addrs.first());
                                if let Some(addr) = pick {
                                    found.lock().unwrap().insert(info.fullname.clone(), *addr);
                                }
                            }
                            ServiceEvent::ServiceRemoved(_, name) => {
                                found.lock().unwrap().remove(&name);
                            }
                            _ => {}
                        }
                    }
                });
                me.daemon = Some(daemon);
                Ok(())
            });
        if let Err(e) = started {
            log::warn!("Wi-Fi iPhone discovery unavailable: {e}");
        }
        me
    }

    pub fn close(self) {
        let udids: Vec<String> = self.devices.lock().unwrap().keys().cloned().collect();
        for udid in udids {
            unmount(&udid);
        }
        if let Some(d) = &self.daemon {
            let _ = d.shutdown();
        }
        if let Some(m) = &self.muxer {
            m.close();
        }
    }

    /// Mounts discovered phones that aren't on USB; returns udid -> mountpoint for live mounts.
    pub fn poll(&mut self, usb_udids: &HashSet<String>) -> HashMap<String, PathBuf> {
        for udid in usb_udids {
            if self.learned.insert(udid.clone()) {
                self.learn(udid);
            }
        }
        let mac_map = self.mac_map();
        let found = self.found.lock().unwrap().clone();
        let mut wanted: HashMap<String, Addr> = HashMap::new();
        for (name, addr) in found {
            if let Some(udid) = mac_map.get(&mac_of(&name))
                && !usb_udids.contains(udid)
                && Path::new(LOCKDOWN_DIR)
                    .join(format!("{udid}.plist"))
                    .is_file()
            {
                wanted.insert(udid.clone(), addr);
            }
        }
        let gone: Vec<String> = {
            let mut devices = self.devices.lock().unwrap();
            let gone = devices
                .keys()
                .filter(|u| !wanted.contains_key(*u))
                .cloned()
                .collect();
            *devices = wanted.clone();
            gone
        };
        for udid in gone {
            unmount(&udid);
        }
        self.ejected.retain(|u| wanted.contains_key(u));

        let mut live = HashMap::new();
        for udid in wanted.keys().filter(|u| !self.ejected.contains(*u)) {
            let mp = mountpoint(udid);
            if mounted(&mp) && dead(&mp) {
                log::warn!("Wi-Fi mount for {udid} stopped responding; remounting");
                unmount(udid);
                self.mount_attempts.insert(udid.clone(), Instant::now());
            } else if mounted(&mp) {
                live.insert(udid.clone(), mp);
            } else if self
                .mount_attempts
                .get(udid)
                .is_none_or(|t| t.elapsed() >= MOUNT_RETRY)
            {
                self.mount_attempts.insert(udid.clone(), Instant::now());
                self.mount(udid, &mp);
            }
        }
        live
    }

    fn mount(&self, udid: &str, mp: &Path) {
        let Some(muxer) = &self.muxer else { return };
        let _ = std::fs::create_dir_all(mp);
        let result = util::spawn_detached(
            Command::new("ifuse")
                .args(["--network", "-u", udid, "-o", "ro"])
                .arg(mp)
                .env(
                    "USBMUXD_SOCKET_ADDRESS",
                    format!("UNIX:{}", muxer.sock_path.display()),
                ),
        );
        if let Err(e) = result {
            log::warn!("ifuse not available: {e}");
        }
    }

    pub fn eject(&mut self, udid: &str) {
        unmount(udid);
        self.ejected.insert(udid.into());
    }

    fn mac_map(&self) -> HashMap<String, String> {
        let mut result = HashMap::new();
        if let Ok(entries) = std::fs::read_dir(LOCKDOWN_DIR) {
            for p in entries
                .flatten()
                .map(|e| e.path())
                .filter(|p| util::suffix(p) == ".plist")
            {
                if let Some(mac) = read_plist(&p)
                    .get("WiFiMACAddress")
                    .and_then(Value::as_string)
                {
                    result.insert(mac.to_lowercase(), util::stem(&p));
                }
            }
        }
        result.extend(self.learned_map());
        result
    }

    fn learned_map(&self) -> HashMap<String, String> {
        std::fs::read(&self.map_file)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }

    fn learn(&self, udid: &str) {
        let out = util::run_capture(
            Command::new("ideviceinfo").args([
                "-u",
                udid,
                "-q",
                "com.apple.mobile.wireless_lockdown",
                "-k",
                "BonjourFullServiceName",
            ]),
            Duration::from_secs(5),
        );
        let Some(out) = out else { return };
        let name = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if !name.contains('@') {
            return;
        }
        let mut known = self.learned_map();
        known.insert(mac_of(&name), udid.into());
        if let Some(p) = self.map_file.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        if let Ok(json) = serde_json::to_string_pretty(&known) {
            let _ = std::fs::write(&self.map_file, json);
        }
    }
}

fn mountpoint(udid: &str) -> PathBuf {
    runtime_dir().join("wifi").join(udid)
}

fn unmount(udid: &str) {
    let mp = mountpoint(udid);
    if mounted(&mp) {
        let _ = util::run_capture(
            Command::new("fusermount3").arg("-uz").arg(&mp),
            Duration::from_secs(10),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sockaddr_layouts() {
        let v4 = sockaddr(("192.168.1.5".parse().unwrap(), 0));
        assert_eq!(v4.len(), 16);
        assert_eq!(&v4[..2], &(libc::AF_INET as u16).to_ne_bytes());
        assert_eq!(&v4[4..8], &[192, 168, 1, 5]);
        let v6 = sockaddr(("fe80::1".parse().unwrap(), 3));
        assert_eq!(v6.len(), 28);
        assert_eq!(&v6[..2], &(libc::AF_INET6 as u16).to_ne_bytes());
        assert_eq!(&v6[24..], &3u32.to_ne_bytes());
    }

    #[test]
    fn dead_mount_detection() {
        assert!(!dead(&std::env::temp_dir()));
        assert!(dead(Path::new("/nonexistent/photo-spool-mount")));
    }

    #[test]
    fn mac_from_service_name() {
        assert_eq!(
            mac_of("AA:BB:CC:DD:EE:FF@fe80::1._apple-mobdev2._tcp.local."),
            "aa:bb:cc:dd:ee:ff"
        );
    }

    #[test]
    fn muxer_protocol_roundtrip() {
        let dir = std::env::temp_dir().join(format!("ps-mux-{}", std::process::id()));
        let devices: Devices = Arc::new(Mutex::new(HashMap::from([(
            "UDID1".to_string(),
            ("10.0.0.2".parse().unwrap(), 0),
        )])));
        let mux = Muxer::start(dir.join("s.sock"), devices).unwrap();
        let mut c = UnixStream::connect(dir.join("s.sock")).unwrap();
        let mut body = Vec::new();
        dict([("MessageType", Value::String("ListDevices".into()))])
            .to_writer_xml(&mut body)
            .unwrap();
        let header: Vec<u8> = [16 + body.len() as u32, 1, 8, 7]
            .iter()
            .flat_map(|w| w.to_le_bytes())
            .collect();
        c.write_all(&[header, body].concat()).unwrap();
        let mut h = [0u8; 16];
        c.read_exact(&mut h).unwrap();
        let len = u32::from_le_bytes(h[..4].try_into().unwrap()) as usize;
        assert_eq!(u32::from_le_bytes(h[12..].try_into().unwrap()), 7);
        let mut resp = vec![0; len - 16];
        c.read_exact(&mut resp).unwrap();
        let d: Dictionary = plist::from_bytes(&resp).unwrap();
        let list = d["DeviceList"].as_array().unwrap();
        let props = list[0].as_dictionary().unwrap()["Properties"]
            .as_dictionary()
            .unwrap();
        assert_eq!(props["SerialNumber"].as_string(), Some("UDID1"));
        assert_eq!(props["NetworkAddress"].as_data().unwrap().len(), 16);
        mux.close();
        let _ = std::fs::remove_dir_all(dir);
    }
}
