"""Tracks currently-attached removable media (SD card readers, the camera
in mass-storage mode, the camera in MTP mode, and iPhones over AFC) as a
live source registry, and drives the automatic-import trigger for sources
that turn out to hold camera media (a DCIM folder -- iPhones expose one
too, via gvfs's AFC backend).

Two connection modes:

- **Mass storage** (SD card reader, or the camera itself in USB Mass
  Storage mode): a normal block device, handled via UDisks2. Mount state
  is reliable here (a real D-Bus property), so this is also where the
  on-demand `mount_source()` call is meaningful -- the source strip can
  show an unmounted card and mount it when the user clicks it.
- **MTP / AFC** (the camera or iPhone plugged in directly): handled via
  gvfs/gio, both the same way -- gvfs exposes each as a
  `mtp:host=...`/`afc:host=...` directory under the runtime gvfs mount
  point once mounted. There's no reliable way to correlate an *unmounted*
  volume (from parsing `gio mount -li` text) with the mounted gvfs
  directory it becomes without real devices to verify the format against
  -- so these sources only enter the registry once actually mounted;
  getting there stays best-effort automatic, not manually triggered (an
  iPhone additionally requires unlocking and accepting the "Trust This
  Computer" prompt on the device itself, which nothing here can bypass --
  the periodic retry just means it mounts on the next poll after that).

Polling rather than subscribing to UDisks2's InterfacesAdded/Removed
signals is a deliberate simplification: those payloads are doubly-nested
D-Bus dicts (`a{oa{sa{sv}}}`) that QtDBus's automatic QVariant conversion
doesn't reliably unpack, and receiving them properly would mean pumping a
second (GLib) event loop alongside Qt's. A ~2.5s poll of
`GetManagedObjects` -- one flat synchronous call -- is trivially reliable
and an imperceptible delay; `poll_now()` also lets the UI force an
immediate check on demand.

Every step degrades quietly: no system bus, no `gio`, no `gvfs-mtp`
backend installed -- each just means that source type is never detected,
never a crash.
"""
from __future__ import annotations

import logging
import os
import shutil
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
GVFS_FUSE_CHECK_RETRY_S = 10

UDISKS_SERVICE = "org.freedesktop.UDisks2"
UDISKS_PATH = "/org/freedesktop/UDisks2"
FS_IFACE = "org.freedesktop.UDisks2.Filesystem"
BLOCK_IFACE = "org.freedesktop.UDisks2.Block"
DRIVE_IFACE = "org.freedesktop.UDisks2.Drive"

DCIM_SEARCH_DEPTH = 2


def _find_dcim(root: Path, depth: int = DCIM_SEARCH_DEPTH) -> bool:
    """Cheap, shallow check for a DCIM folder -- used only to decide
    whether to fire the *automatic* silent-import trigger, not as the
    real file search (the scanner does the recursive walk) and not to
    gate whether a mounted source shows up in the strip at all. Also
    matches when `root` itself is already named DCIM -- an iPhone's
    reported root is pre-narrowed to its DCIM folder (see
    _narrow_iphone_root), so the child-search below would otherwise never
    match and auto-import would silently never fire for one."""
    if root.name.upper() == "DCIM" and root.is_dir():
        return True
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if entry.is_dir() and entry.name.upper() == "DCIM":
            return True
    if depth > 0:
        for entry in entries:
            if entry.is_dir() and _find_dcim(entry, depth - 1):
                return True
    return False


def _narrow_iphone_root(root: str) -> str:
    """An iPhone's AFC root also exposes PhotoData/Mutations/... --
    internal iOS Photos-library bookkeeping (Live Photo edit derivatives
    like ".../Adjustments/FullSizeRender.mov", etc.), not real importable
    originals. Confirmed live against a real iPhone: scanning the whole
    AFC root pulled these in as false-positive "videos" (matching
    VIDEO_EXTENSIONS by name alone) and 679 of 693 "found" files failed
    with a plain ENOENT -- PhotoData/Mutations entries are inconsistent/
    semi-virtual over AFC, not stable files. Scoping to DCIM (present on
    every iPhone) fixes both the false positives and the reliability hit
    from touching that subtree at all. Deliberately iPhone-only: a Sony
    camera's video files live in PRIVATE/M4ROOT, a sibling of (not
    inside) DCIM, so narrowing blockdev/MTP sources the same way would
    silently drop real footage."""
    dcim = Path(root) / "DCIM"
    return str(dcim) if dcim.is_dir() else root


def _gvfs_dir() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime_dir) / "gvfs"


def _gvfs_fuse_mounted(gvfs_dir: Path) -> bool:
    """Whether $XDG_RUNTIME_DIR/gvfs is actually a live gvfsd-fuse mount --
    not just whether a gvfsd-fuse *process* happens to exist, which could
    be stale/unrelated. `gio mount` succeeding is not enough on its own:
    confirmed against a real iPhone that `gio` reported its AFC share
    mounted fine (readable via `gio info`) while this FUSE directory
    stayed completely empty the whole time, because nothing had started
    gvfsd-fuse at all."""
    target = str(gvfs_dir)
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            return any(
                line.split()[1] == target and line.split()[2] == "fuse.gvfsd-fuse"
                for line in fh if len(line.split()) > 2
            )
    except OSError:
        return False


def _ensure_gvfs_fuse_running(gvfs_dir: Path) -> None:
    """GNOME sessions start gvfsd-fuse automatically (D-Bus/autostart
    machinery this app can't rely on); a bare compositor session (e.g.
    niri, confirmed on this machine) doesn't start it at all, so `gio
    mount`-ing an MTP/AFC device works at the GVfs API level but the
    device never appears as a real file to anything using plain POSIX
    paths -- which is everything downstream of this module. gvfs-fuse
    ships only the binary (no unit, no autostart entry), so self-starting
    it here is the reliable fix rather than depending on the desktop
    environment to have arranged it -- same philosophy as dnglab's
    self-download in dnglab_setup.py."""
    gvfs_dir.mkdir(parents=True, exist_ok=True)
    if _gvfs_fuse_mounted(gvfs_dir):
        return
    gvfsd_fuse = shutil.which("gvfsd-fuse") or "/usr/libexec/gvfsd-fuse"
    if not Path(gvfsd_fuse).is_file():
        return  # gvfs-fuse isn't installed -- nothing to do, degrade quietly
    try:
        subprocess.Popen(
            [gvfsd_fuse, str(gvfs_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        logger.warning("Could not start gvfsd-fuse", exc_info=True)


def _gvfs_fallback_label(gvfs_dirname: str) -> str:
    # Used only if _gio_display_name() couldn't find a proper name. Dirnames
    # look like "mtp:host=SonyCorporation_ILCE_7RM3_..." or
    # "afc:host=00008130-0012345A6B78901C" -- not worth fully decoding,
    # just make it slightly more readable than the raw gvfs name.
    for prefix in ("mtp:host=", "afc:host="):
        if gvfs_dirname.startswith(prefix):
            host = gvfs_dirname.removeprefix(prefix)
            return host.split("_")[0].replace("%20", " ") or "Device"
    return gvfs_dirname


def _gio_display_name(path: Path) -> str:
    """The human-readable device/mount name gvfs itself knows (e.g. "SONY
    ILCE-7RM3" or "Lachlan's iPhone"), read straight off the already-
    discovered FUSE path via `gio info`. Simpler and more reliable than
    the previous approach of cross-referencing `gio mount -li`'s free-text
    device name against a dirname reconstructed from its
    `default_location=` URI: confirmed broken against a real iPhone --
    that URI always ends in "/", so its last path segment came back empty
    (silently dropping every name), and even fixed, the segment is just
    the bare device UDID, never the "afc:host=..."/"mtp:host=..." form
    the FUSE directory actually uses. LC_ALL=C keeps the "display name:"
    label itself parseable regardless of the system locale (only the
    value -- the device's own name -- should ever be localized)."""
    try:
        result = subprocess.run(
            ["gio", "info", "-a", "standard::display-name", str(path)],
            capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("display name:"):
            return line.split(":", 1)[1].strip()
    return ""


class DeviceWatcher(QObject):
    # Fired once per physical insertion, only for sources confirmed to
    # hold camera media -- drives the automatic-import path.
    sourceFound = Signal(str, str, str)  # root_path, label, kind ('blockdev' | 'mtp')

    # Live registry, for the source strip.
    sourceAdded = Signal(str, str, str, bool, str)  # key, label, kind, mounted, root
    sourceRemoved = Signal(str)  # key
    sourceMountChanged = Signal(str, bool, str)  # key, mounted, root

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mtp_enabled = True
        self._sources: dict[str, dict] = {}  # key -> {label, kind, mounted, root, camera_media}
        self._auto_mount_attempted: set[str] = set()  # blockdev keys
        self._auto_import_fired: set[str] = set()  # keys that already triggered sourceFound
        self._mtp_mount_attempts: dict[str, float] = {}  # activation uri -> last attempt time
        self._gvfs_fuse_last_check = 0.0
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

    def poll_now(self) -> None:
        """Manual "refresh" trigger -- same poll a timer tick runs."""
        self._poll()

    def sources(self) -> list[dict]:
        """Snapshot of the current registry, e.g. to seed a model on
        startup (the added/removed signals only cover changes after
        whoever's listening connects)."""
        return [
            {"key": key, "label": s["label"], "kind": s["kind"], "mounted": s["mounted"], "root": s["root"] or ""}
            for key, s in self._sources.items()
        ]

    def _poll(self) -> None:
        try:
            self._poll_blockdevs()
        except Exception:
            logger.warning("UDisks2 poll failed", exc_info=True)
        if self._mtp_enabled:
            try:
                self._poll_gvfs_devices()
            except Exception:
                logger.warning("gvfs (MTP/AFC) poll failed", exc_info=True)

    # --- SD card reader / camera mass-storage mode ---------------------------------

    def _poll_blockdevs(self) -> None:
        if self._bus is None:
            return
        manager = dbus.Interface(
            self._bus.get_object(UDISKS_SERVICE, UDISKS_PATH), "org.freedesktop.DBus.ObjectManager",
        )
        objects = manager.GetManagedObjects()

        current_keys: set[str] = set()
        for obj_path, ifaces in objects.items():
            key = str(obj_path)
            if FS_IFACE not in ifaces:
                continue

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
            current_keys.add(key)

            mount_points = [
                bytes(bytearray(p)).rstrip(b"\x00").decode("utf-8", "replace")
                for p in ifaces[FS_IFACE].get("MountPoints", [])
            ]
            root = mount_points[0] if mount_points else None
            label = drive_label or (Path(root).name if root else key.rsplit("/", 1)[-1]) or "Removable media"

            existing = self._sources.get(key)
            if existing is None:
                self._sources[key] = {"label": label, "kind": "blockdev", "mounted": root is not None,
                                       "root": root, "camera_media": None}
                self.sourceAdded.emit(key, label, "blockdev", root is not None, root or "")
                if root is None and key not in self._auto_mount_attempted:
                    self._auto_mount_attempted.add(key)
                    root = self._mount_blockdev(obj_path)
                    if root:
                        self._sources[key]["mounted"] = True
                        self._sources[key]["root"] = root
                        self.sourceMountChanged.emit(key, True, root)
                if root:
                    self._maybe_fire_auto_import(key, root, label, "blockdev")
            else:
                if existing["mounted"] != (root is not None) or existing["root"] != root:
                    existing["mounted"] = root is not None
                    existing["root"] = root
                    self.sourceMountChanged.emit(key, root is not None, root or "")
                if root and existing["camera_media"] is None:
                    self._maybe_fire_auto_import(key, root, label, "blockdev")

        for key in [k for k, s in self._sources.items() if s["kind"] == "blockdev" and k not in current_keys]:
            del self._sources[key]
            self._auto_mount_attempted.discard(key)
            self._auto_import_fired.discard(key)
            self.sourceRemoved.emit(key)

    def _mount_blockdev(self, obj_path) -> str | None:
        try:
            fs = dbus.Interface(self._bus.get_object(UDISKS_SERVICE, obj_path), FS_IFACE)
            return str(fs.Mount({}))
        except Exception:
            logger.info("Could not mount %s", obj_path, exc_info=True)
            return None

    def mount_source(self, key: str) -> str | None:
        """On-demand mount, e.g. the user clicking an unmounted card's icon
        in the source strip. Blockdev only -- see module docstring for why
        MTP/AFC can't offer this reliably here."""
        source = self._sources.get(key)
        if source is None or source["kind"] != "blockdev" or source["mounted"]:
            return source["root"] if source else None
        if self._bus is None:
            return None
        root = self._mount_blockdev(key)
        if root:
            source["mounted"] = True
            source["root"] = root
            self.sourceMountChanged.emit(key, True, root)
            self._maybe_fire_auto_import(key, root, source["label"], "blockdev")
        return root

    def _maybe_fire_auto_import(self, key: str, root: str, label: str, kind: str) -> None:
        has_dcim = _find_dcim(Path(root))
        self._sources[key]["camera_media"] = has_dcim
        if has_dcim and key not in self._auto_import_fired:
            self._auto_import_fired.add(key)
            self.sourceFound.emit(root, label, kind)

    # --- camera plugged in as MTP, or an iPhone over AFC ------------------------------

    def _poll_gvfs_devices(self) -> None:
        gvfs_dir = _gvfs_dir()
        now = time.monotonic()
        if now - self._gvfs_fuse_last_check >= GVFS_FUSE_CHECK_RETRY_S:
            self._gvfs_fuse_last_check = now
            _ensure_gvfs_fuse_running(gvfs_dir)
        try:
            # An AFC dirname with ",port=" is a specific app's sandboxed
            # Documents folder (the same thing Finder/Files shows as "On
            # My iPhone" per-app sharing), not the device's main share --
            # confirmed against a real iPhone: never has a DCIM folder,
            # just noise (a photo-less "device" card) for this app.
            gvfs_dirs = {
                p.name for p in gvfs_dir.iterdir()
                if p.name.startswith(("mtp:host=", "afc:host="))
                and not (p.name.startswith("afc:host=") and ",port=" in p.name)
            } if gvfs_dir.is_dir() else set()
        except OSError:
            gvfs_dirs = set()

        current_keys = {f"gvfs-dir:{name}" for name in gvfs_dirs}
        for name in gvfs_dirs:
            key = f"gvfs-dir:{name}"
            root = str(gvfs_dir / name)
            label = _gio_display_name(Path(root)) or _gvfs_fallback_label(name)
            # "iphone" is only ever used for the in-memory registry/source
            # strip (a nicer icon) -- sourceFound below always reports
            # "mtp" so it stays a value the sessions table's CHECK
            # constraint actually allows.
            display_kind = "iphone" if name.startswith("afc:host=") else "mtp"
            if display_kind == "iphone":
                root = _narrow_iphone_root(root)
            if key not in self._sources:
                self._sources[key] = {"label": label, "kind": display_kind, "mounted": True, "root": root,
                                       "camera_media": None}
                self.sourceAdded.emit(key, label, display_kind, True, root)
                self._maybe_fire_auto_import(key, root, label, "mtp")
            else:
                self._sources[key]["label"] = label
                if self._sources[key]["camera_media"] is None:
                    self._maybe_fire_auto_import(key, root, label, "mtp")

        for key in [k for k, s in self._sources.items()
                    if s["kind"] in ("mtp", "iphone") and k not in current_keys]:
            del self._sources[key]
            self._auto_import_fired.discard(key)
            self.sourceRemoved.emit(key)

        # gvfs only auto-mounts a detected volume if something enables
        # automount (default under GNOME; not guaranteed under a bare
        # compositor like niri) -- explicitly mount anything we see listed
        # but not yet under gvfs_dir, retrying periodically since a camera
        # or phone may need the user to accept a prompt on its own screen
        # first (an iPhone's "Trust This Computer?").
        now = time.monotonic()
        for uri in self._list_unmounted_gvfs_uris():
            last = self._mtp_mount_attempts.get(uri, 0.0)
            if now - last < MTP_MOUNT_RETRY_S:
                continue
            self._mtp_mount_attempts[uri] = now
            try:
                subprocess.Popen(["gio", "mount", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

        # An iPhone's *main* AFC share (the one with DCIM on it) never
        # actually appears as a listed, mountable Volume in `gio mount
        # -li` at all -- confirmed live, both on first connect and again
        # after a physical unplug/replug: only per-app "Files" document
        # shares (",port=" dirnames) show up there and auto-mount on
        # their own. The main share has to be mounted by its well-known
        # afc://<udid>/ URI directly, which is why _list_unmounted_gvfs_uris
        # above can never find it. idevice_id (usbmuxd) reports physically
        # connected UDIDs independent of gvfs's own incomplete volume
        # enumeration, so use that instead to know what to try mounting.
        for udid in self._list_connected_iphone_udids():
            if f"afc:host={udid}" in gvfs_dirs:
                continue  # main share already mounted
            attempt_key = f"afc-main:{udid}"
            last = self._mtp_mount_attempts.get(attempt_key, 0.0)
            if now - last < MTP_MOUNT_RETRY_S:
                continue
            self._mtp_mount_attempts[attempt_key] = now
            try:
                subprocess.Popen(
                    ["gio", "mount", f"afc://{udid}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass

    def _list_connected_iphone_udids(self) -> set[str]:
        try:
            result = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return set()
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def _list_unmounted_gvfs_uris(self) -> set[str]:
        try:
            result = subprocess.run(["gio", "mount", "-li"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return set()
        if result.returncode != 0:
            return set()
        uris: set[str] = set()
        in_relevant_volume = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Volume(") or stripped.startswith("Mount("):
                in_relevant_volume = False  # new block; type line (if any) comes next
            elif stripped.startswith("Type:"):
                # MTP backend: "GProxyVolumeMonitorMTP"; AFC (iPhone)
                # backend naming isn't proxied the same way MTP/UDisks2
                # are, so match loosely rather than one exact string.
                in_relevant_volume = "MTP" in stripped or "Afc" in stripped or "AFC" in stripped
            elif in_relevant_volume and stripped.startswith("activation_root="):
                uris.add(stripped.split("=", 1)[1].strip())
        return uris

    # --- ejecting ---------------------------------------------------------------------

    def eject(self, root_path: str) -> bool:
        key = next((k for k, s in self._sources.items() if s["root"] == root_path), None)
        source = self._sources.get(key) if key else None
        if source is not None and source["kind"] == "blockdev" and self._bus is not None:
            try:
                fs = dbus.Interface(self._bus.get_object(UDISKS_SERVICE, key), FS_IFACE)
                fs.Unmount({})
                source["mounted"] = False
                source["root"] = None
                self.sourceMountChanged.emit(key, False, "")
                return True
            except Exception:
                logger.info("Unmount via UDisks2 failed for %s", root_path, exc_info=True)
                return False
        try:
            subprocess.run(["gio", "mount", "-u", root_path], capture_output=True, timeout=15)
            if key:
                del self._sources[key]
                self.sourceRemoved.emit(key)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
