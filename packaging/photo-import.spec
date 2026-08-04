Name:           photo-import
Version:        0.1.0
Release:        1%{?dist}
Summary:        Automatic ARW -> lossless DNG photo import for Sony cameras

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
# Optional: camera-as-MTP-device support (backend/device_watch.py degrades
# gracefully -- SD-card-reader import still works fully without it).
Recommends:     gvfs-mtp
# Optional: raw-Wayland compositor-blur detection (progressive enhancement,
# app works fine without it -- see backend/blur.py).
Recommends:     python3-pywayland

%description
A personal, local-only Wayland desktop app that watches for an SD card or a
Sony camera being connected (mass storage or MTP) and automatically imports
new ARW files: converts them to lossless-compressed DNG (via a self-managed
dnglab binary, downloaded on first use -- no Adobe DNG Converter needed on
Linux) and files them into the existing photo library using the same
YYYY/YYYY-MM/YYYY-MM-DD/YYYY.MM.DD_Model_NNNNN.dng convention Lightroom was
already using. Content-hash deduplication means re-scanning a card that
still has old photos on it never re-imports anything. Native desktop
notifications, XDG-portal-aware light/dark theming, no telemetry, no
network access beyond the one-time dnglab download.

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
* Tue Aug 04 2026 Photo Import <noreply@example.com> - 0.1.0-1
- Initial release: UDisks2 + MTP device detection, ARW scanning and
  content-hash dedup, self-downloaded dnglab lossless DNG conversion,
  Lightroom-matching library layout, notifications, session history UI.
