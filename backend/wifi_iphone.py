"""iPhones over Wi-Fi: mDNS discovery (`_apple-mobdev2._tcp`) plus a minimal
usbmuxd stand-in socket, so stock `ifuse --network` can mount a paired phone
read-only. Stock usbmuxd has no network support; libusbmuxd only needs
ListDevices/ReadPairRecord/ReadBUID from the muxer, libimobiledevice then
dials the phone's address itself.

The mDNS name starts with the phone's Wi-Fi MAC, which iOS may randomise per
network, so the MAC->UDID map is learned from lockdown's
BonjourFullServiceName whenever the phone is on USB, falling back to the pair
record's WiFiMACAddress."""

import ipaddress
import json
import logging
import os
import plistlib
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import paths

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_apple-mobdev2._tcp.local."
LOCKDOWN_DIR = Path("/var/lib/lockdown")
MOUNT_RETRY_S = 15.0

try:
    from zeroconf import ServiceBrowser, Zeroconf
except ImportError:
    Zeroconf = None


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "photo-spool"


def sockaddr(ip: str) -> bytes:
    """Native Linux struct sockaddr_in/_in6, which libimobiledevice copies verbatim."""
    addr = ipaddress.ip_address(ip.split("%")[0])
    if addr.version == 4:
        return struct.pack("=H", socket.AF_INET) + bytes(2) + addr.packed + bytes(8)
    scope = socket.if_nametoindex(ip.split("%")[1]) if "%" in ip else 0
    return struct.pack("=H", socket.AF_INET6) + bytes(6) + addr.packed + struct.pack("=I", scope)


def mac_of(service_name: str) -> str:
    return service_name.split("@", 1)[0].lower()


def _read_plist(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return {}


def _mounted(mountpoint: Path) -> bool:
    # /proc/mounts, not os.path.ismount: stat on a dead network FUSE mount can block
    try:
        with open("/proc/self/mounts") as fh:
            return any(line.split()[1] == str(mountpoint) for line in fh)
    except OSError:
        return False


class Muxer:
    def __init__(self, sock_path: Path, devices: dict[str, str], lock: threading.Lock):
        self.sock_path = sock_path
        self._devices = devices
        self._lock = lock
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        sock_path.unlink(missing_ok=True)
        self._server = socket.socket(socket.AF_UNIX)
        self._server.bind(str(sock_path))
        self._server.listen()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()
        self.sock_path.unlink(missing_ok=True)

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _device_list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._devices.items())
        return [
            {"DeviceID": i, "MessageType": "Attached", "Properties": {
                "ConnectionType": "Network", "DeviceID": i, "SerialNumber": udid, "NetworkAddress": sockaddr(ip)}}
            for i, (udid, ip) in enumerate(items, 1)
        ]

    def reply(self, req: dict) -> list[dict]:
        kind = req.get("MessageType")
        if kind == "ListDevices":
            return [{"DeviceList": self._device_list()}]
        if kind == "ReadPairRecord":
            udid = str(req.get("PairRecordID", ""))
            path = LOCKDOWN_DIR / f"{udid}.plist"
            if "/" not in udid and path.is_file():
                return [{"PairRecordData": path.read_bytes()}]
        if kind == "ReadBUID":
            buid = _read_plist(LOCKDOWN_DIR / "SystemConfiguration.plist").get("SystemBUID")
            if buid:
                return [{"BUID": buid}]
        if kind == "Listen":
            return [{"MessageType": "Result", "Number": 0}, *self._device_list()]
        return [{"MessageType": "Result", "Number": 1}]

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            while True:
                header = conn.recv(16, socket.MSG_WAITALL)
                if len(header) < 16:
                    return
                length, _version, _msg, tag = struct.unpack("<IIII", header)
                try:
                    req = plistlib.loads(conn.recv(length - 16, socket.MSG_WAITALL))
                except Exception:
                    return
                for i, msg in enumerate(self.reply(req)):
                    body = plistlib.dumps(msg)
                    conn.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, tag if i == 0 else 0) + body)


class WifiIphones:
    """Owned by DeviceWatcher; `poll()` runs on its GUI-thread timer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._found: dict[str, str] = {}  # mdns name -> address
        self._devices: dict[str, str] = {}  # udid -> address, what the muxer advertises
        self._mount_attempts: dict[str, float] = {}
        self._learned: set[str] = set()
        self._ejected: set[str] = set()
        self._map_file = paths.data_home() / "wifi-iphones.json"
        self._zc = None
        self._muxer = None
        if Zeroconf is None:
            return
        try:
            self._muxer = Muxer(runtime_dir() / "usbmuxd.sock", self._devices, self._lock)
            self._zc = Zeroconf()
            ServiceBrowser(self._zc, SERVICE_TYPE, self)
        except Exception:
            logger.warning("Wi-Fi iPhone discovery unavailable", exc_info=True)

    def close(self) -> None:
        for udid in list(self._devices):
            self._unmount(udid)
        if self._zc is not None:
            self._zc.close()
        if self._muxer is not None:
            self._muxer.close()

    # zeroconf ServiceListener, called on zeroconf's browser thread
    def add_service(self, zc, type_, name) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        addrs = info.parsed_scoped_addresses() if info else []
        if addrs:
            v4 = [a for a in addrs if ":" not in a]
            with self._lock:
                self._found[name] = (v4 or addrs)[0]

    update_service = add_service

    def remove_service(self, zc, type_, name) -> None:
        with self._lock:
            self._found.pop(name, None)

    def mountpoint(self, udid: str) -> Path:
        return runtime_dir() / "wifi" / udid

    def poll(self, usb_udids: set[str]) -> dict[str, Path]:
        """Mount discovered phones that aren't on USB; return udid -> mountpoint for live mounts."""
        for udid in usb_udids - self._learned:
            self._learned.add(udid)
            self._learn(udid)
        mac_map = self._mac_map()
        with self._lock:
            found = dict(self._found)
        wanted = {}
        for name, addr in found.items():
            udid = mac_map.get(mac_of(name))
            if udid and udid not in usb_udids and (LOCKDOWN_DIR / f"{udid}.plist").is_file():
                wanted[udid] = addr
        with self._lock:
            gone = set(self._devices) - set(wanted)
            self._devices.clear()
            self._devices.update(wanted)
        for udid in gone:
            self._unmount(udid)
        self._ejected &= set(wanted)

        live = {}
        now = time.monotonic()
        for udid in set(wanted) - self._ejected:
            mp = self.mountpoint(udid)
            if _mounted(mp):
                live[udid] = mp
            elif now - self._mount_attempts.get(udid, 0.0) >= MOUNT_RETRY_S:
                self._mount_attempts[udid] = now
                self._mount(udid, mp)
        return live

    def _mount(self, udid: str, mp: Path) -> None:
        mp.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, USBMUXD_SOCKET_ADDRESS=f"UNIX:{self._muxer.sock_path}")
        try:
            subprocess.Popen(["ifuse", "--network", "-u", udid, "-o", "ro", str(mp)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            logger.warning("ifuse not available", exc_info=True)

    def _unmount(self, udid: str) -> None:
        mp = self.mountpoint(udid)
        if _mounted(mp):
            subprocess.run(["fusermount3", "-uz", str(mp)], capture_output=True, timeout=10)

    def eject(self, udid: str) -> None:
        self._unmount(udid)
        self._ejected.add(udid)

    def _mac_map(self) -> dict[str, str]:
        result = {}
        for path in LOCKDOWN_DIR.glob("*.plist"):
            mac = _read_plist(path).get("WiFiMACAddress")
            if mac:
                result[mac.lower()] = path.stem
        try:
            result.update(json.loads(self._map_file.read_text()))
        except (OSError, ValueError):
            pass
        return result

    def _learn(self, udid: str) -> None:
        try:
            out = subprocess.run(
                ["ideviceinfo", "-u", udid, "-q", "com.apple.mobile.wireless_lockdown", "-k", "BonjourFullServiceName"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return
        if "@" not in out:
            return
        try:
            known = json.loads(self._map_file.read_text())
        except (OSError, ValueError):
            known = {}
        known[mac_of(out)] = udid
        self._map_file.parent.mkdir(parents=True, exist_ok=True)
        self._map_file.write_text(json.dumps(known, indent=1))
