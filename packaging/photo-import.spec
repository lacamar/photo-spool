Name:           photo-import
Version:        0.2.20
Release:        1%{?dist}
Summary:        Automatic raw -> lossless DNG photo import from cameras and iPhones

# Personal/local tool; MIT is just a permissive default -- change freely.
License:        MIT
URL:            https://github.com/example/photo-import
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  desktop-file-utils
BuildRequires:  librsvg2-tools
BuildRequires:  python3
BuildRequires:  systemd-rpm-macros

Requires:       python3
Requires:       python3-pyside6
Requires:       python3-dbus
Requires:       glib2
Requires:       udisks2
Requires:       perl-Image-ExifTool
Requires:       ffmpeg-free
Requires:       xdg-utils
Requires:       hicolor-icon-theme
# Camera-as-MTP and iPhone-as-AFC support are advertised, first-class
# features (not an optional edge case), so these are hard Requires, not
# Recommends -- confirmed live against a real iPhone that a system with
# install_weak_deps=False (this machine's own dnf.conf) silently never
# installed gvfs-mtp/gvfs-afc at all via a plain Recommends, and that
# gvfs-fuse (needed for an MTP/AFC mount to appear as a real POSIX path
# to anything, not just be visible to `gio`) was missing from this spec
# entirely. SD-card-reader import still works without any of these
# (backend/device_watch.py degrades quietly), only MTP/AFC detection
# needs them.
Requires:       gvfs-mtp
Requires:       gvfs-afc
Requires:       gvfs-fuse
# idevice_id -- an iPhone's main AFC share (the one with DCIM) never
# appears as a listed, mountable Volume in `gio mount -li` at all, so
# device_watch.py uses this (independent of gvfs's own volume
# enumeration) to know when to proactively mount it by its well-known
# afc://<udid>/ URI. Without it, only the per-app document shares
# auto-mount, and the device looks undetected after every replug.
Requires:       libimobiledevice-utils
# Optional: raw-Wayland compositor-blur detection (progressive enhancement,
# app works fine without it -- see backend/blur.py).
Recommends:     python3-pywayland

%description
A personal, local-only Wayland desktop app that watches for an SD card, a
camera (mass storage or MTP), or an iPhone (AFC) being connected, and
imports new raw photos: ARW/CR2/CR3/NEF/RAF/RW2/ORF/PEF are converted to
lossless-compressed DNG (via a self-managed dnglab binary, downloaded on
first use -- no Adobe DNG Converter needed on Linux); files already in DNG
form (e.g. iPhone ProRAW) are copied straight through. Video files
(MP4/MOV/M4V/MTS/M2TS/AVI) are filed alongside the stills too, untouched
-- just renamed into place, no conversion. Everything is filed into the
existing photo library using the same
YYYY/YYYY-MM/YYYY-MM-DD/YYYY.MM.DD_Model_NNNNN convention Lightroom was
already using (with each file's own original extension). A source strip
shows every attached device plus manually
pinned folders as clickable icons; opening one shows a thumbnail picker
with already-imported shots greyed out (or markable as already-imported
without re-processing them) and everything else preselected. Content-hash
deduplication means re-scanning a card that still has old photos on it
never re-imports anything. Native desktop notifications, XDG-portal-aware
light/dark theming, no telemetry, no network access beyond the one-time
dnglab download.

%prep
%autosetup -n %{name}-%{version}

%build
# Pure Python + QML: nothing to compile. Rasterize the app icon from its
# SVG source at the sizes the hicolor theme expects.
mkdir -p render
for size in 16 32 48 64 128 256 512; do
    rsvg-convert -w "${size}" -h "${size}" icons/%{name}.svg -o "render/%{name}-${size}.png"
done

%install
install -Dm755 packaging/%{name} %{buildroot}%{_bindir}/%{name}

install -d %{buildroot}%{_datadir}/%{name}
cp -a main.py backend qml icons %{buildroot}%{_datadir}/%{name}/

desktop-file-install --dir=%{buildroot}%{_datadir}/applications \
    --set-key=Exec --set-value=%{name} \
    --set-key=Icon --set-value=%{name} \
    %{name}.desktop

install -Dm644 icons/%{name}.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
for size in 16 32 48 64 128 256 512; do
    install -Dm644 "render/%{name}-${size}.png" \
        "%{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps/%{name}.png"
done

# A *user* unit (needs the Wayland session/D-Bus user bus), not a system
# one -- installed but left disabled by default (no preset ships), so
# `systemctl --user enable --now photo-import.service` is an opt-in step,
# not a surprise background app after a routine package upgrade.
install -Dm644 packaging/%{name}.service %{buildroot}%{_userunitdir}/%{name}.service

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{name}.desktop
find %{buildroot}%{_datadir}/%{name} -name '*.py' -print0 | xargs -0 python3 -c '
import ast, sys
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        ast.parse(fh.read(), path)
'

%post
%systemd_user_post %{name}.service
# Immediately restart for every currently logged-in user who has it
# enabled/running, so installing an update actually takes effect right
# away instead of waiting for the user to remember to do it by hand.
# try-restart is a safe no-op for anyone who never enabled it.
loginctl list-users --no-legend 2>/dev/null | while read -r uid user _; do
    [ -S "/run/user/${uid}/bus" ] || continue
    XDG_RUNTIME_DIR="/run/user/${uid}" runuser -u "$user" -- \
        systemctl --user try-restart %{name}.service >/dev/null 2>&1 || :
done

%preun
%systemd_user_preun %{name}.service

%postun
%systemd_user_postun_with_restart %{name}.service

%files
%{_bindir}/%{name}
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/*/apps/%{name}.png
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
%{_userunitdir}/%{name}.service

%changelog
* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.20-1
- Fixed the iPhone appearing undetected again after a physical unplug/
  replug: confirmed live that `gio mount -li` never lists the phone's
  *main* AFC share (the one with DCIM on it) as a discoverable Volume at
  all -- only per-app "Files" document shares show up there and
  auto-mount on their own. device_watch.py now uses `idevice_id` (new
  Requires: libimobiledevice-utils) to detect physically-connected
  iPhones independent of gvfs's own incomplete volume listing, and
  proactively mounts the main share by its well-known afc://<udid>/ URI.
  Verified live: a freshly unmounted device recovers within one ~2s poll
  cycle, with no manual intervention.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.19-1
- Fixed a real iPhone import attempt mostly failing (679 of 693 files,
  "No such file or directory"): the AFC scan walked the phone's entire
  root, including PhotoData/Mutations/... (iOS's internal Live-Photo-edit
  bookkeeping), whose entries are inconsistent/semi-virtual over AFC and
  happened to match VIDEO_EXTENSIONS by name (e.g. ".../Adjustments/
  FullSizeRender.mov"). The reported source root is now scoped to DCIM
  when present -- iPhone-only, not blockdev/MTP, since a Sony camera's
  video lives in PRIVATE/M4ROOT, a sibling of (not inside) DCIM.
- Any session still marked "running" at startup (a crash, an OOM kill, or
  a service restart landing mid-scan -- all three seen for real while
  debugging the above) is now reconciled to "failed" instead of leaving
  a permanently-stuck "running" card in the history forever.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.18-1
- Fixed iPhone detection not working at all, root-caused live against a
  real device:
  - gvfs-mtp/gvfs-afc were never actually installed on this machine --
    install_weak_deps=False in dnf.conf means a plain Recommends silently
    does nothing. Promoted both to hard Requires, since MTP/AFC support
    is an advertised core feature, not an optional edge case.
  - Even with those installed, gio mounted the iPhone's AFC share fine
    at the GVfs API level, but it never appeared as a real file under
    $XDG_RUNTIME_DIR/gvfs, because gvfsd-fuse was never running --
    nothing starts it automatically outside a full GNOME session (a bare
    niri session, confirmed on this machine, has no such autostart, and
    gvfs-fuse itself ships only the binary, no unit/autostart entry).
    device_watch.py now checks /proc/mounts and self-starts gvfsd-fuse if
    it's not already serving that directory -- same self-managed-
    dependency approach as dnglab's self-download. Added gvfs-fuse as a
    new Requires too.
  - Fixed the device's display name coming back empty/falling back to
    the raw UDID: the old lookup cross-referenced `gio mount -li`'s
    free-text name against a dirname reconstructed from a
    default_location= URI, which breaks for AFC (that URI always ends in
    "/", so the reconstructed dirname came back empty) and wasn't
    actually correlatable to the real "afc:host=..." directory name to
    begin with. Now reads the name directly off the already-discovered
    path via `gio info`, which is both simpler and correct.
  - An iPhone's "Files > On My iPhone" per-app document shares (e.g.
    every installed app's sandboxed Documents folder) were also showing
    up as their own separate, photo-less "device" card -- filtered out,
    since only the device's main AFC share ever has a DCIM folder.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.17-1
- The systemd service now launches quietly: new --start-hidden flag
  (main.py) skips showing the window on launch (just the tray icon),
  wired into photo-import.service's ExecStart. A manual launch (desktop
  icon, terminal) is unaffected -- the window still opens immediately as
  before. Previously the service opened the window on every start, same
  as a normal launch.
- %post now immediately restarts the service (systemctl --user
  try-restart, a no-op for anyone who never enabled it) for every
  currently logged-in user, so a package upgrade actually takes effect
  right away instead of requiring a manual restart or waiting for next
  login.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.16-1
- Added a systemd *user* service (packaging/photo-import.service):
  starts the app in the background and restarts it (Restart=on-failure,
  capped at 5 restarts/60s) if it crashes. Tied to graphical-session.target
  since it needs the Wayland/D-Bus user session, same as niri itself.
  Installed but not enabled by default (no preset ships, and %post/%preun/
  %postun use the standard %systemd_user_* RPM macros) -- a routine
  package upgrade should never silently start a new background app, so
  run `systemctl --user enable --now photo-import.service` to opt in.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.15-1
- Fixed missing camera-model metadata on imported videos: some cameras
  don't embed a Model tag in video the way they do in stills (confirmed
  against this machine's own library -- pre-existing videos like
  "2022.06.03__221935228.mp4" already show the resulting empty-model gap
  in their filename). If every other file with a known model in the same
  scan agrees on exactly one (covers Sony's photos/video living in
  separate directory trees on one card), that model is now borrowed for
  the video's filename/dedup key and written into the placed video file
  itself via exiftool. Never infers per-file variable fields (date/time,
  lens) this way, and backs off entirely on an ambiguous/mixed-camera
  batch rather than guessing wrong. Verified against real library files.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.14-1
- "Mark as already imported" is now quiet: it no longer creates a
  history card, an in-app notification, or a native desktop notification
  -- it just records the dedup ledger entry in the background
  (sessions.mark_only, filtered out of the history list/status bar).
- Fixed a latent bug this surfaced: imports.session_id was NOT NULL with
  ON DELETE CASCADE, so clearing a session from history (added in 0.2.10)
  silently deleted its dedup-ledger rows too, meaning cleared files would
  look "new" again on the next scan. session_id is now nullable / ON
  DELETE SET NULL, so the ledger (and the new stats below) survive
  clearing history.
- Source picker: the "Mark unselected as already imported" bulk action
  is now "Mark selected as already imported", using the same checkbox
  selection the "Import N selected" button already uses -- select once,
  then choose which of the two actions applies to that selection.
- New library stats page (📊 in the header): lifetime totals (files,
  storage, photos vs. videos, date range) and a per-camera breakdown,
  computed from the persistent dedup ledger so it survives history
  clears.
- Fixed a MouseArea stacking bug in the source strip: the whole-tile
  click-to-open area was declared after (and so on top of) a pinned
  folder's "✕" remove button, silently swallowing every click on it --
  removing a folder has never actually worked until this fix.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.13-1
- Tray icon behavior split by click: left-click (Trigger) now checks for
  new importable media (appController.refreshDevices()) and confirms with
  a native tray balloon instead of toggling the window; opening the
  window is now exclusively the right-click menu's "Open Photo Import"
  action, so the two never fight over what a click does.
- Video files now get a real thumbnail: they have no embedded-preview tag
  for exiftool to read (confirmed empty against real iPhone .MOV and
  camera .mp4 files), so the thumbnail provider now decodes one actual
  frame via ffmpeg for video paths, used by both the source-picker grid
  and the session-detail list. New Requires: ffmpeg-free.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.12-1
- Video files (MP4/MOV/M4V/MTS/M2TS/AVI) are now imported alongside
  stills: discovered, deduped, and filed into the library using the same
  YYYY.MM.DD_Model_NNNNN naming convention, but never touched by dnglab
  -- just renamed and copied through with their original extension, same
  passthrough path DNG-source files already used. Metadata reads now fall
  back to CreateDate when a file has no DateTimeOriginal (the normal case
  for video), so dates/folders come out right for those too. The new
  DNGBackwardVersion rewrite from 0.2.11 no longer runs against non-DNG
  destinations.
- Fixed two real QML bugs surfaced by an actual run's stderr log: the
  source picker's selection checkmark warned "Unable to assign
  [undefined] to bool" for any file whose selection-map entry had been
  removed (e.g. after "Have it"); and clicking a notification threw
  "model is not defined" and silently failed to navigate to its session,
  because markNotificationRead's synchronous model reset was destroying
  the clicked delegate's context before the next line read it.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.11-1
- Every DNG landing in the library (both dnglab's own conversions and
  DNG-passthrough files like iPhone ProRAW) now has its DNGBackwardVersion
  tag rewritten to 1.4.0.0 in place via exiftool right after it's placed,
  for compatibility with tools that don't understand newer DNG spec
  versions -- same fix as the existing standalone
  ~/.local/bin/dng-version-converter script, now applied automatically at
  import time instead of as a separate manual pass. Non-fatal if exiftool
  fails on a given file -- the file stays correctly placed either way.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.10-1
- Session detail rows: default click now opens the file itself in the
  desktop's default image viewer (xdg-open on the file); a new small
  folder button at the right end of each row reveals the file selected in
  the file manager, via the freedesktop org.freedesktop.FileManager1
  ShowItems D-Bus method -- xdg-open can only ever target a folder, never
  select an item inside it.
- Import history: each session card now has a small dismiss control to
  clear that one entry, plus a header trash-can button to clear all
  finished sessions at once. Clearing only forgets the app's own history
  record (DB row, cascades to its per-file rows) -- it never touches the
  imported photos on disk. A currently-running session can't be cleared.
- User-facing copy no longer singles out Sony ARW -- "raw files"/"raw
  photos" throughout, matching the app's actual multi-format support.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.9-1
- Subtle motion polish: popups (settings/notifications/session detail) now
  fade+scale in and out instead of snapping instantly -- QtQuick Controls'
  Basic style Popup has no default transition at all. The session and
  notification lists now animate new items in and reflow existing ones
  instead of jumping. The header bell badge does a small pop when the
  unread count changes, and header icon buttons give a subtle press-down
  scale for tactile feedback.
- Investigated real Wayland background blur via the ext-background-effect-v1
  protocol (niri supports it) -- not implemented: requesting a blur region
  requires the window's raw wl_surface pointer, which PySide6 6.11 doesn't
  expose to Python (only the display-wide native interface, not per-window).
  Would need a compiled C extension; not pursued for now. The existing
  translucent-background fallback is unchanged.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.8-1
- Fixed a duplicate-import bug: since 0.2.4 (run-in-background/tray icon),
  nothing stopped a second launch from starting its own independent
  instance -- each with its own DeviceWatcher auto-importing from the same
  attached card, racing to import the same photos and landing on
  "(2)"/"(3)"/"(4)" duplicate filenames. The app now enforces a single
  running instance (QLocalServer/QLocalSocket lock in main.py): a second
  launch just raises the existing window instead of starting duplicate
  background workers.
- Hardened the import pipeline against the underlying race regardless:
  if two imports ever do land on the same source_hash concurrently, the
  loser now discards its copy and records a normal "duplicate" instead of
  crashing the session and leaving an orphan file on disk.

* Thu Aug 06 2026 Photo Import <noreply@example.com> - 0.2.7-1
- Thumbnails (session detail view and the source picker grid) now have
  true squircle (superellipse) corners instead of a plain circular
  border-radius -- rendered via a new SquircleImage component (Canvas-
  drawn mask + MultiEffect GPU compositing).
- Session detail rows now highlight on hover, matching their new
  clickability (open-in-file-browser).
- Source picker tile's filename caption is now an inset rounded chip
  instead of a bar flush against the tile edges, so it doesn't clash with
  the new corner curve.

* Wed Aug 05 2026 Photo Import <noreply@example.com> - 0.2.6-1
- App version is now shown at the bottom of the Settings page
  (backend/__init__.py's __version__ is the single source of truth --
  bump it alongside this spec's Version on every release).

* Wed Aug 05 2026 Photo Import <noreply@example.com> - 0.2.5-1
- Fixed blank/unclickable thumbnails in the session detail view for
  duplicate files caught by the fast (no-hash) pre-check: that path was
  recording an empty dest_path instead of looking up the existing file's
  actual location, so both the thumbnail and click-to-open (both gated on
  dest_path) silently did nothing for those rows.
- The "importing new photos..." desktop notification now sets the
  freedesktop "transient" hint, so the desktop's own notification
  daemon/history panel doesn't retain it either (our own in-app history
  skip from 0.2.4 only affected this app's history, not the OS one).

* Wed Aug 05 2026 Photo Import <noreply@example.com> - 0.2.4-1
- App now runs in the background: closing the main window hides it instead
  of quitting, and a system tray icon (left-click to show/hide, right-click
  for Show/Quit) is the only way to actually exit besides Ctrl+C.
- Session detail view (the imported-files list) now shows a thumbnail per
  row and clicking a row opens its destination folder in the default file
  browser via xdg-open.
- Raw-format support widened further: Canon CRM (Cinema RAW Light),
  Olympus ORI, Panasonic/Leica RAW/RWL, Hasselblad FFF, Sigma X3F, and
  Apple QuickTake QTK, matching dnglab's full supported-extension list.
- "X: importing new photos..." is now a transient desktop notification only
  (no longer written to the in-app notification history), so plugging in a
  device repeatedly doesn't clutter the history panel; import-complete and
  error notifications still persist as before.

* Wed Aug 05 2026 Photo Import <noreply@example.com> - 0.2.3-1
- Added a pause/resume control for imports (top status bar): pauses the
  import queue at the next safe boundary (between files during the
  scan/hash/place phases, before a conversion batch starts, or before the
  next queued device starts at all) rather than mid-file. New/auto-detected
  cards still get queued while paused, just not processed until resumed.
- Raw-format support widened from a curated subset to every format dnglab
  itself supports (Sony SRF/SR2, Canon CRW, Nikon NRW, Minolta MRW,
  Samsung SRW, Epson ERF, Kodak KDC/DCS/DCR, Hasselblad 3FR, Mamiya MEF,
  Phase One/Leaf IIQ/MOS, ARRI ARI, alongside the existing ARW/CR2/CR3/
  NEF/RAF/RW2/ORF/PEF/DNG) -- a format dnglab can't actually decode just
  fails that one file with dnglab's own error, never a crash.
- Verified DNG-passthrough (already-DNG files, e.g. iPhone ProRAW, copy
  straight through with no reconversion) and iPhone/MTP-kind import
  sessions end-to-end against isolated test fixtures.
- Source strip cards now show a device/directory kind label (e.g. "SD
  card / storage", "iPhone", "Camera (MTP)", "Folder") alongside the
  existing photo counts and capacity bar.

* Tue Aug 04 2026 Photo Import <noreply@example.com> - 0.2.2-1
- Fixed the thumbnail preview grid showing filenames/checkboxes but every
  image tile blank: PySide6's QQuickImageProvider.requestImage binding
  requires returning a bare QImage (with the out-size written into the
  passed-in `size` argument), not a (QImage, QSize) tuple as the C++
  pointer-out-param signature suggests -- the tuple form failed silently
  (stderr RuntimeWarning only).
- Source strip cards now show live stats instead of just an icon and
  label: total/not-yet-imported photo counts and a capacity bar (free of
  total), refreshed automatically on mount, manual refresh, and after an
  import completes.

* Tue Aug 04 2026 Photo Import <noreply@example.com> - 0.2.1-1
- Fixed the source strip device cards being completely unclickable: the
  sourcesModel's "root" role name shadowed SourceStrip.qml's own `id: root`
  (ListView delegates auto-expose model roles as bare identifiers), so
  root.sourceClicked(...) silently threw a QML TypeError on every click
  instead of opening the preview picker. Renamed the role to "rootPath".

* Tue Aug 04 2026 Photo Import <noreply@example.com> - 0.2.0-1
- Source strip: live devices (SD card/camera/iPhone) plus pinned folders as
  clickable icons; thumbnail picker with per-photo/bulk selection and a
  "mark as already imported" action that skips conversion entirely.
- Multi-format raw support (CR2/CR3/NEF/RAF/RW2/ORF/PEF alongside ARW) and
  DNG passthrough (copy, no reconversion) for iPhone ProRAW and similar.
- iPhone support via AFC (gvfs-afc), alongside existing MTP/mass-storage.
- Phased, per-file progress reporting (scanning/checking/converting/placing)
  plus a persistent top-of-window status bar for the active/queued import --
  fixes imports appearing to hang with no feedback during the (genuinely
  slow, card-reader-bound) hashing phase.
- Manual device refresh and on-demand mount-when-clicked for unmounted cards.
- RPM build switched to mx-rpm (mock-based), matching this machine's other
  packages.

* Tue Aug 04 2026 Photo Import <noreply@example.com> - 0.1.0-1
- Initial release: UDisks2 + MTP device detection, ARW scanning and
  content-hash dedup, self-downloaded dnglab lossless DNG conversion,
  Lightroom-matching library layout, notifications, session history UI.
