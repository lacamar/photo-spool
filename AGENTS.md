# CLAUDE.md

- COPR: lacamar/arm64-misc
- Release: `packaging/build-rpm.sh -S --copr lacamar/arm64-misc --copr-wait`

## What this is

**Photo Spool** (`photo-spool`; formerly `photo-import`, renamed 0.3.0; Python/PySide6 until 0.5.0, Rust since
0.6.0) is a personal, local-only Wayland desktop app (niri) that replaces Lightroom's import step: it watches for an
SD card, a camera (mass storage or MTP) or an iPhone (AFC, or Wi-Fi), converts raws to lossless DNG with the
system-packaged `dnglab`, and files everything into
`~/Pictures/YYYY/YYYY-MM/YYYY-MM-DD/YYYY.MM.DD_Model_NNNNN.ext`. Shares its QML style (`Theme.qml`,
`qml/components/`) with `social-crm`.

Rust backend + unchanged QML UI via **cxx-qt**. `just run | demo | test | lint | clean-db`.

## Layout

- `src/` core, Qt-free and unit-tested: `scanner` (discovery, exiftool metadata + `metadata_cache`, dedup
  lookups), `converter` (dnglab batch, dest naming), `import` (pipeline + worker thread), `workers`
  (preview/stats), `devices` (UDisks2 + gvfs poll), `wifi` (mDNS + usbmuxd stand-in), `desktop` (zbus:
  notifications, inhibit, portal, FileManager1), `thumbs`, `db` (migrations, settings), `paths`.
- `src/qt/bridge.rs` declares every QML-facing type: `AppController`, `SessionListModel`, `SourceListModel`.
  Impls live in `controller.rs` / `models.rs`. `src/qt/shim.rs` + `cpp/shim.cpp` hold the only C++:
  `QApplication` and the `image://thumb` provider (Qt shrinks and encodes; Rust extracts and caches).
- `build.rs` registers QML module `PhotoSpool` (Rust types + `Theme.qml` singleton) and compiles every other
  `.qml` into qrc under the same prefix; `Main.qml` loads from `qrc:/qt/qml/PhotoSpool/qml/Main.qml` and
  instantiates the controller and both models itself, then calls `appController.start()`.

## Architecture rules

- **One thread per blocking job, one sqlite connection per thread.** Import, preview, stats and the device
  poll each run on their own `std::thread`; results reach Qt only through `qt_thread().queue(...)` onto the
  controller. New backend capabilities go through `AppController`, never called from QML directly.
- Import is phased (`scanning → checking → converting → placing`): hashing is the slow part (~1s/file), so
  progress must stay phase-aware. Pause takes effect only between files or before a dnglab batch.
- Every source file is read exactly once: hashed while staged into `$XDG_DATA_HOME/photo-spool/staging`, then
  moved into place. Sources are never written (except the opt-in delete-originals setting).
- Dedup is two-tier: `(camera_model, filename, size)` pre-check, then sha256. Metadata (and model inference)
  is read over the whole card before narrowing to a selection.
- `iphone` is a display-only source kind; sessions record `mtp` (`sessions.kind` CHECK constraint).
- Migrations in `db.rs` are append-only.
- Model role `rootPath`, never `root`: delegate roles shadow `id: root` silently (stderr-only TypeError).
- iPhone over Wi-Fi: live-tested on iOS 26.7.2 over the phone's hotspot; lockdown works but `com.apple.afc`
  refuses connections; untested on a shared LAN.

## Testing

`cargo test` (also `%check` in the spec). Every test touching paths, DB or settings holds
`testutil::Isolated`, which points all `XDG_*` vars at a temp dir under a global lock; this is load-bearing
(an early suite once wrote into the real `~/Pictures`). Tests use a fake `dnglab` shell script and the real
exiftool.

Never run the app against real XDG paths, `$HOME` or attached hardware: the device watcher talks to the real
system bus and auto-mounts/auto-imports cards. Manual runs use scratch `XDG_*`, `TMPDIR` and
`XDG_RUNTIME_DIR` (keep paths short: the instance socket needs < 108 chars), `QT_QPA_PLATFORM=offscreen`,
and both `DBUS_*_BUS_ADDRESS` pointed at a dead socket. For interactive checks, ask the user to run it and
paste stderr; QML errors only show on stderr.

## Packaging

`packaging/build-rpm.sh` archives HEAD and `cargo vendor`s crates into a second source tarball (mock builds
are offline). Bump `Version:` in the spec and `Cargo.toml` together (a test enforces it), add a changelog
entry, tag `vX.Y.Z`. `dnf` installs prompt through polkit; confirm with `rpm -q photo-spool`. A system with the
old `photo-import` service enabled needs one `systemctl --user enable --now photo-spool.service`.
