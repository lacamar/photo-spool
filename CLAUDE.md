# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Photo Spool** (package/service/module name: `photo-spool`; formerly `photo-import`, renamed 0.3.0) is a
personal, local-only Wayland desktop app (PySide6/QML, niri compositor) that replaces Lightroom's import
step: watches for an SD card, a camera (mass storage or MTP), or an iPhone (AFC), and imports new raw
photos — converting to lossless-compressed DNG via the system-packaged `dnglab` binary and filing them into
`~/Pictures/YYYY/YYYY-MM/YYYY-MM-DD/YYYY.MM.DD_Model_NNNNN.dng`. Sibling project to `social-crm`; shares its
toolkit and QML visual style (`Theme.qml` and the `qml/components/` building blocks follow the same
conventions).

## Commands

```bash
just run          # python3 main.py
just demo         # python3 main.py --demo   (seeds demo session history if the DB is empty)
just clean-db     # wipe the local sqlite DB ($XDG_DATA_HOME/photo-spool/data.db)
just test         # python3 -m unittest discover -s tests -v
./run.sh          # equivalent to `just run`, usable outside the repo root
```

There's a real automated test suite under `tests/` (stdlib `unittest`, no extra dependency) covering
`scanner.py`, `converter.py`, `settings_store.py`, `db.py`'s migrations, and `ImportWorker._run_session`
called directly (never via `.start()`/a real `QThread`, and never against real hardware). Every test case
inherits `tests.testutil.IsolatedTestCase`, which points every `XDG_*` var at a fresh temp directory for
that test — **this is load-bearing, not a nicety**: an early version of this suite wrote real fake `.dng`
files into the real `~/Pictures` because `settings_store.DEFAULTS["library_root"]` used to be computed once
at module-import time (before any test's isolation took effect) instead of fresh per call — see
`tests/test_settings_store.py`'s regression coverage for that exact bug and `settings_store._default_for`
for the fix. Never write a test that calls `paths`/`settings_store`/`db` functions before
`IsolatedTestCase.setUp()` has run. `no automated test suite` used to be true here; it no longer is, and
`packaging/photo-spool.spec`'s `%check` now runs this suite as part of every RPM build — a failing test
fails the build, same as the existing `desktop-file-validate`/syntax checks there.

**Never write a test that imports `backend.app_controller` or `backend.device_watch`** — both touch the
real system D-Bus (and `DeviceWatcher` real attached hardware) regardless of `XDG_*` overrides, the same
hazard "Testing discipline" below describes for manual harness scripts. `ImportWorker`/`scanner`/
`converter`/`db`/`settings_store` are all safely testable in isolation; the device-detection and GUI layers
are not, and stay covered by the manual/live-verification pattern below instead.

To sanity-check Python syntax after editing without launching the GUI:
```bash
python3 -c "import ast; ast.parse(open('backend/whatever.py').read())"
```

### Packaging

```bash
./packaging/build-rpm.sh          # builds via mx-rpm (mock-based), not plain rpmbuild
sudo dnf install -y ~/.local/rpm/rpms/photo-spool/photo-spool-<version>-1.fc45.noarch.rpm
```

`packaging/photo-spool.spec` is the in-repo canonical spec; `build-rpm.sh` symlinks it into
`~/.local/rpm/specs/photo-spool.spec` and stages the source tarball (via `git archive`, tracked files
only) under `~/.local/rpm/sources/photo-spool/` -- mx-rpm's own default source-root resolution, no
`--sourceroot` override -- matching this machine's convention for other RPM-packaged projects.
**Versioning convention: bump `Version:` + add a `%changelog` entry in the spec, and create a
matching annotated git tag (`git tag -a vX.Y.Z`), for every meaningful round of changes** — don't skip this
even for small fixes.

The package was renamed from `photo-import` to `photo-spool` at 0.3.0 (spec `Provides`/`Obsoletes` handle the
`dnf` upgrade path; `backend/paths.py`'s `_migrate_data_home` moves the old `$XDG_DATA_HOME/photo-import`
directory in place on first run under the new name). A system that already had the old service enabled needs
one manual `systemctl --user enable --now photo-spool.service` after upgrading — the new package name means
`%post`'s "only auto-enable on first install" logic doesn't carry the old unit's enabled state forward.

`sudo dnf install` on this machine goes through a graphical `pkexec`/polkit prompt, not a terminal password
prompt — if a backgrounded install command hangs with no output, it's very likely stuck waiting on that
dialog, not actually failing. Check `rpm -q photo-spool` (not just the shell exit code — a command piped
through anything like `| tail` reports the pipe's exit code, not `dnf`'s) to confirm whether it actually
went through.

## Architecture

### Threading model

Every long-running or blocking operation gets its own `QThread` with its **own sqlite3 connection**
(connections aren't safe to share across threads) — never block the GUI thread on device I/O, hashing, or
subprocess calls:

- **`ImportWorker`** (`import_worker.py`) — a `queue.Queue`-backed FIFO; one `ImportRequest` (one
  device/folder) processed at a time. Supports pause/resume via a `QMutex`/`QWaitCondition` pair that's
  checked at safe boundaries (between files in the checking/placing loops, before a conversion batch
  starts, before the next queued session starts) — never mid-file, since neither a single hash read nor a
  single dnglab batch invocation is safely interruptible.
- **`PreviewWorker`** (`preview_worker.py`) — "latest request wins" (a `QWaitCondition` with a single
  pending slot, superseded on each new request), for the source-picker thumbnail grid. Only one preview
  panel is ever open at a time, so an in-flight scan for a panel the user already navigated away from is
  just discarded.
- **`SourceStatsWorker`** (`stats_worker.py`) — a FIFO dict-queue (dedups by source key, preserves
  insertion order), *not* latest-wins — several source cards can legitimately want their stats refreshed
  together and none should cancel another.
- **`DeviceWatcher`** (`device_watch.py`) — polls rather than subscribes to D-Bus signals (UDisks2's
  `InterfacesAdded` payload is a doubly-nested dict QtDBus doesn't reliably unmarshal); a `QTimer` on the
  GUI thread ticks every ~2.5s, calling one flat synchronous `GetManagedObjects` via `dbus-python` (not
  QtDBus). `poll_now()` gives the UI a manual-refresh hook.
- **`dnglab_setup`** — no worker thread here; `dnglab` is a system package (`Requires: dnglab` in the
  photo-spool spec) so `dnglab_setup.find_existing()` is just a synchronous `shutil.which` lookup, cheap
  enough to call straight from the GUI thread (at startup, and from `retryDnglabSetup()` after the user
  installs the package).

`AppController` (`app_controller.py`) is the single QObject facade exposed to QML as `appController`; it
owns the GUI-thread DB connection, wires every worker's signals to the list models and to its own
`Property`/`Signal` pairs (e.g. `activeSessionId`/`activePhase`/... for the top status bar), and is the only
place backend modules and QML-facing state meet. New backend capabilities get threaded through here, not
called directly from QML.

`shutdown()` (connected to `app.aboutToQuit`) must stop every worker thread and `wait()` on it before the
DB connection closes — Python destroying a still-running `QThread` at interpreter exit is a hard Qt crash,
not a graceful shutdown.

### Import pipeline (`import_worker.py`)

Phased, not a single flat counter — `PHASE_SCANNING` → `PHASE_CHECKING` → `PHASE_CONVERTING` →
`PHASE_PLACING`, reported via `sessionProgress`. This matters because hashing (the dedup step) is the
genuinely slow part in practice (~1s/file, card-reader-bound), while dnglab conversion itself is
sub-second/file — without phase-aware progress the checking phase looks hung.

- Dedup is two-tier: a cheap `(camera_model, filename, size_bytes)` pre-check
  (`scanner.quick_duplicate_check`) before ever hashing, then authoritative sha256 content-hash dedup
  (`scanner.hash_file` / `hash_duplicate_check`) only for files that pass the pre-check.
- Every source file is hashed and staged (a real copy, not a symlink — see `scanner.hash_and_stage`) into a
  scratch dir exactly once, whether it ends up converted or passed through, so a slow source (confirmed
  live: an iPhone's AFC mount) is never read twice. Files already in DNG form (`scanner.is_dng`) or videos
  skip dnglab entirely and are `shutil.move`d straight from that staged copy into the library — the
  original source file itself is never touched either way. Everything else's staged copy is run through one
  batched `dnglab convert -r` invocation (`converter.convert_batch`), whose `-v` output is streamed to
  report per-file progress, and the converted output is then moved into place the same way.
- `RAW_EXTENSIONS` in `scanner.py` is dnglab's *entire* supported-format list (confirmed against dnglab's
  own docs), not a curated subset — a format dnglab can't actually decode just fails that one file with
  dnglab's own error text, never crashes the batch.
- iPhones report `kind="iphone"` only in the in-memory source registry/UI (nicer icon); everywhere a
  session or DB row is involved it collapses to `"mtp"` (the `sessions.kind` CHECK constraint only allows
  `blockdev`/`mtp`/`manual`) — see the `{"folder": "manual", "iphone": "mtp"}` mapping in
  `app_controller.py`.

### Source strip / device registry

`DeviceWatcher` tracks two independent connection modes into one registry: **block devices** (SD card
readers, camera in mass-storage mode) via UDisks2 — mount state is a real D-Bus property, so on-demand
`mount_source()` is meaningful here — and **MTP/AFC** (camera or iPhone plugged in directly) via
`gio`/gvfs directory watching under `$XDG_RUNTIME_DIR/gvfs/`. MTP/AFC sources only ever enter the registry
once actually mounted (no reliable way to correlate an unmounted `gio mount -li` volume with the gvfs
directory it becomes), so there's no manual-mount path for those — gvfs auto-mount is nudged along by a
periodic best-effort `gio mount <uri>` retry instead.

`SourceListModel` (`list_models.py`) backs the source strip's cards. Each card also carries live stats
(`SourceStatsWorker` output: photo count, not-yet-imported count, capacity/used/free bytes) that get
invalidated back to "unloaded" on any mount-state change and re-requested on mount, manual refresh, and
after an import session finishes for that source.

### QML/Python boundary

- `Theme.qml` is registered as a singleton (`qmlRegisterSingletonType`), not a context property.
- Every other piece of backend state reaches QML as a context property set in `main.py`: `appController`
  plus the two list models (`sessionModel`, `sourcesModel`).
- `backend/thumbnail_provider.py` serves `image://thumb/<url-encoded-path>` via a `QQuickImageProvider`
  registered with `engine.addImageProvider`.

### Two sharp, non-obvious PySide6/QML footguns hit in this codebase

Both are documented in-place with a comment where they were hit — read them before touching either area
again:

1. **Model-role/`id:` shadowing** (`list_models.py`, `SourceStrip.qml`): a `ListView`/`GridView` delegate
   auto-exposes every model role as a bare identifier in scope. A role literally named `root` silently
   shadows a component's own `id: root`, so `root.someSignal(...)` resolves to the role's *string value*
   instead of the component and throws a `TypeError` visible only in stderr — no visual sign, screenshots
   show nothing. This is why `SourceListModel`'s mount-path role is named `rootPath`, not `root`.
2. **`QQuickImageProvider.requestImage` return shape** (`thumbnail_provider.py`): PySide6's binding wants a
   bare `QImage` return with the out-size written into the passed-in `size` argument
   (`size.setWidth(...)`/`setHeight(...)`) — *not* a `(QImage, QSize)` tuple, even though that's what the
   C++ pointer-out-param signature suggests. The tuple form fails silently (a stderr `RuntimeWarning` only);
   every `Image` element just sits in `Image.Error` state with no visible sign.

General lesson from both: when a QML-facing interaction "does nothing" with no visible error, get real
stderr (run from a terminal) before spending time on rendering/timing theories — synthetic
`QTest.mouseClick`-style automated testing has also proven unreliable in this environment (fails even for
trivially-correct isolated cases), so it's not reliable evidence of anything either way.

### Testing discipline

**Never test against real XDG paths, real `$HOME`, or real attached hardware.** `DeviceWatcher` connects to
the real system D-Bus regardless of any `XDG_*` override, so even an "isolated" run can see and act on a
genuinely attached SD card/camera/iPhone. Past sessions have had near-misses (a first test launch with
default settings almost auto-imported a real 330-file SD card into the real library before being caught
mid-hash).

The established safe pattern for verifying a change (used repeatedly, e.g. to test the thumbnail provider
fix or the pause/resume behavior): a throwaway script under a scratchpad dir that
- sets `XDG_DATA_HOME`/`XDG_CACHE_HOME`/`XDG_CONFIG_HOME`/`XDG_PICTURES_DIR` to scratch paths *before*
  importing `backend.paths` or anything that reads them,
- either avoids constructing the real `AppController`/`DeviceWatcher` entirely (build a minimal fake
  `QObject` controller wired to the real backend workers/models against fake local folders — see git
  history for `cardtest.py`-style harnesses) or drives `ImportWorker`/other workers directly with synthetic
  `ImportRequest`s against scratch source/library directories,
- runs under `QT_QPA_PLATFORM=offscreen` for any QML/visual check (`grabWindow()`/`grabToImage()` to a PNG,
  read back with the Read tool) — no real window ever appears,
- is deleted after use; nothing under a scratchpad dir is meant to persist.

If real interactive verification is unavoidable (e.g. confirming a click actually works), ask the user to
run the app from a terminal themselves and paste back stderr, rather than trying to automate it.
