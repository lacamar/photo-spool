Name:           photo-import
Version:        0.2.3
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

Requires:       python3
Requires:       python3-pyside6
Requires:       python3-dbus
Requires:       glib2
Requires:       udisks2
Requires:       perl-Image-ExifTool
Requires:       xdg-utils
Requires:       hicolor-icon-theme
# Optional: camera/phone-as-MTP-or-AFC-device support (backend/device_watch.py
# degrades gracefully -- SD-card-reader import still works fully without them).
Recommends:     gvfs-mtp
Recommends:     gvfs-afc
# Optional: raw-Wayland compositor-blur detection (progressive enhancement,
# app works fine without it -- see backend/blur.py).
Recommends:     python3-pywayland

%description
A personal, local-only Wayland desktop app that watches for an SD card, a
camera (mass storage or MTP), or an iPhone (AFC) being connected, and
imports new raw photos: ARW/CR2/CR3/NEF/RAF/RW2/ORF/PEF are converted to
lossless-compressed DNG (via a self-managed dnglab binary, downloaded on
first use -- no Adobe DNG Converter needed on Linux); files already in DNG
form (e.g. iPhone ProRAW) are copied straight through. Everything is filed
into the existing photo library using the same
YYYY/YYYY-MM/YYYY-MM-DD/YYYY.MM.DD_Model_NNNNN.dng convention Lightroom was
already using. A source strip shows every attached device plus manually
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

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{name}.desktop
find %{buildroot}%{_datadir}/%{name} -name '*.py' -print0 | xargs -0 python3 -c '
import ast, sys
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        ast.parse(fh.read(), path)
'

%files
%{_bindir}/%{name}
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/*/apps/%{name}.png
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg

%changelog
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
