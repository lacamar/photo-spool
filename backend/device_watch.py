"""Detects newly-connected SD cards / cameras and reports a scannable root
path for each, covering two connection modes:

- **Mass storage** (SD card reader, or the camera itself in USB Mass
  Storage mode): a normal block device, handled via UDisks2.
- **MTP** (the camera plugged in directly, in its default USB mode):
  handled via gvfs/gio.

Both are polled on a timer rather than driven by D-Bus signals. For
UDisks2 this is a deliberate simplification: `InterfacesAdded`'s payload is
a doubly-nested D-Bus dict (`a{oa{sa{sv}}}`) that QtDBus's automatic
QVariant conversion doesn't reliably unpack, and receiving it properly
would mean pumping a second (GLib) event loop alongside Qt's. A ~2.5s poll
of `GetManagedObjects` -- one flat synchronous call -- is trivially
reliable and an imperceptible delay for "I just plugged in a card." MTP
has no signal to subscribe to in the first place from here, so it was
always going to be a poll; we just use the same timer for both.

Every step degrades quietly: no system bus, no `gio`, no `gvfs-mtp`
backend installed -- each just means that source type is never detected,
never a crash.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

try:
    import dbus
except ImportError:  # pragma: no cover - dbus-python always present via dnf dep
    dbus = None

logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 2500
MTP_MOUNT_RETRY_S = 12

UDISKS_SERVICE = "org.freedesktop.UDisks2"
UDISKS_PATH = "/org/freedesktop/UDisks2"
FS_IFACE = "org.freedesktop.UDisks2.Filesystem"
BLOCK_IFACE = "org.freedesktop.UDisks2.Block"
DRIVE_IFACE = "org.freedesktop.UDisks2.Drive"

DCIM_SEARCH_DEPTH = 2


def _looks_like_camera_media(root: Path, depth: int = DCIM_SEARCH_DEPTH) -> bool:
    """Cheap, shallow check for a DCIM folder -- used only to avoid
    treating an unrelated USB drive as an import source, not as the
    definitive file search (the scanner does the real recursive walk)."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if entry.is_dir() and entry.name.upper() == "DCIM":
            return True
    if depth > 0:
        for entry in entries:
            if entry.is_dir() and _looks_like_camera_media(entry, depth - 1):
                return True
    return False


def _gvfs_dir() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime_dir) / "gvfs"


def _mtp_label(gvfs_dirname: str) -> str:
    # Dirnames look like "mtp:host=SonyCorporation_ILCE_7RM3_..." -- not
    # worth fully decoding, just make it slightly more readable than the
    # raw gvfs name.
    host = gvfs_dirname.removeprefix("mtp:host=")
    return host.split("_")[0].replace("%20", " ") or "Camera (MTP)"


class DeviceWatcher(QObject):
    sourceFound = Signal(str, str, str)  # root_path, label, kind ('blockdev' | 'mtp')

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mtp_enabled = True
        self._seen_blockdev: set[str] = set()  # UDisks2 filesystem object paths
        self._seen_mtp: set[str] = set()  # gvfs dir names under .../gvfs/
        self._mtp_mount_attempts: dict[str, float] = {}  # activation uri -> last attempt time
        self._ejectable: dict[str, tuple[str, str]] = {}  # root_path -> (kind, handle)
        self._bus = None
        if dbus is not None:
            try:
                self._bus = dbus.SystemBus()
            except Exception:
                logger.warning("Could not connect to system bus for UDisks2", exc_info=True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def start(self, mtp_enabled: bool = True) -> None:
        self._mtp_enabled = mtp_enabled
        self._poll()
        self._timer.start(POLL_INTERVAL_MS)

    def stop(self) -> None:
        self._timer.stop()

    def set_mtp_enabled(self, enabled: bool) -> None:
        self._mtp_enabled = enabled

    def _poll(self) -> None:
        try:
            self._poll_blockdevs()
        except Exception:
            logger.warning("UDisks2 poll failed", exc_info=True)
        if self._mtp_enabled:
            try:
                self._poll_mtp()
            except Exception:
                logger.warning("MTP poll failed", exc_info=True)

    # --- SD card reader / camera mass-storage mode ---------------------------------

    def _poll_blockdevs(self) -> None:
        if self._bus is None:
            return
        manager = dbus.Interface(
            self._bus.get_object(UDISKS_SERVICE, UDISKS_PATH), "org.freedesktop.DBus.ObjectManager",
        )
        objects = manager.GetManagedObjects()
        current_paths = {str(p) for p, ifaces in objects.items() if FS_IFACE in ifaces}

        for obj_path, ifaces in objects.items():
            obj_path = str(obj_path)
            if FS_IFACE not in ifaces or obj_path in self._seen_blockdev:
                continue
            self._seen_blockdev.add(obj_path)

            block = ifaces.get(BLOCK_IFACE, {})
            drive_path = block.get("Drive")
            removable = False
            drive_label = ""
            if drive_path and str(drive_path) != "/":
                drive_ifaces = objects.get(drive_path, {}).get(DRIVE_IFACE, {})
                removable = bool(drive_ifaces.get("Removable", False)) or bool(
                    drive_ifaces.get("MediaRemovable", False)
                )
                drive_label = str(drive_ifaces.get("Model", "") or "")
            if not removable:
                continue

            mount_points = [
                bytes(bytearray(p)).rstrip(b"\x00").decode("utf-8", "replace")
                for p in ifaces[FS_IFACE].get("MountPoints", [])
            ]
            root = mount_points[0] if mount_points else self._mount_blockdev(obj_path)
            if not root:
                continue
            root_path = Path(root)
            if not _looks_like_camera_media(root_path):
                continue
            self._ejectable[root] = ("blockdev", obj_path)
            self.sourceFound.emit(root, drive_label or root_path.name or "Removable media", "blockdev")

        self._seen_blockdev &= current_paths

    def _mount_blockdev(self, obj_path) -> str | None:
        try:
            fs = dbus.Interface(self._bus.get_object(UDISKS_SERVICE, obj_path), FS_IFACE)
            return str(fs.Mount({}))
        except Exception:
            logger.info("Could not auto-mount %s", obj_path, exc_info=True)
            return None

    # --- camera plugged in as MTP -----------------------------------------------------

    def _poll_mtp(self) -> None:
        gvfs_dir = _gvfs_dir()
        try:
            mtp_dirs = {p.name for p in gvfs_dir.iterdir() if p.name.startswith("mtp:host=")} \
                if gvfs_dir.is_dir() else set()
        except OSError:
            mtp_dirs = set()

        for name in mtp_dirs - self._seen_mtp:
            self._seen_mtp.add(name)
            root = gvfs_dir / name
            if not _looks_like_camera_media(root):
                continue
            self._ejectable[str(root)] = ("mtp", str(root))
            self.sourceFound.emit(str(root), _mtp_label(name), "mtp")
        self._seen_mtp &= mtp_dirs

        # gvfs only auto-mounts a detected MTP volume if something enables
        # automount (default under GNOME; not guaranteed under a bare
        # compositor like niri) -- explicitly mount anything we see listed
        # but not yet under gvfs_dir, retrying periodically since a camera
        # may need the user to accept a prompt on its own screen first.
        now = time.monotonic()
        for uri in self._list_unmounted_mtp_uris():
            last = self._mtp_mount_attempts.get(uri, 0.0)
            if now - last < MTP_MOUNT_RETRY_S:
                continue
            self._mtp_mount_attempts[uri] = now
            try:
                subprocess.Popen(["gio", "mount", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def _list_unmounted_mtp_uris(self) -> set[str]:
        try:
            result = subprocess.run(["gio", "mount", "-li"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return set()
        if result.returncode != 0:
            return set()
        uris: set[str] = set()
        in_mtp_volume = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Volume(") or stripped.startswith("Mount("):
                in_mtp_volume = False  # new block; type line (if any) comes next
            elif stripped.startswith("Type:"):
                in_mtp_volume = "GProxyVolumeMonitorMTP" in stripped
            elif in_mtp_volume and stripped.startswith("activation_root="):
                uris.add(stripped.split("=", 1)[1].strip())
        return uris

    # --- ejecting ---------------------------------------------------------------------

    def eject(self, root_path: str) -> bool:
        kind, handle = self._ejectable.get(root_path, (None, None))
        if kind == "blockdev" and self._bus is not None:
            try:
                fs = dbus.Interface(self._bus.get_object(UDISKS_SERVICE, handle), FS_IFACE)
                fs.Unmount({})
                self._ejectable.pop(root_path, None)
                return True
            except Exception:
                logger.info("Unmount via UDisks2 failed for %s", root_path, exc_info=True)
                return False
        try:
            subprocess.run(["gio", "mount", "-u", root_path], capture_output=True, timeout=15)
            self._ejectable.pop(root_path, None)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
