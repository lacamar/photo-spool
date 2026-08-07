"""Discovers raw photo files on a source root and extracts the metadata
needed to name and deduplicate them. Read-only: touches neither the
destination library nor the DB (dedup lookups are plain queries the
caller runs)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EXIFTOOL_BATCH_TIMEOUT_S = 180
HASH_CHUNK_SIZE = 1024 * 1024

# Every raw format dnglab (rawler) itself claims to support, per its own
# README -- not a curated subset -- plus DNG itself (iPhone ProRAW saves
# natively as DNG, as do some cameras; see `is_dng` below for why that one
# gets different handling downstream). A format landing in this set but not
# actually decodable by the installed dnglab just fails that one file with
# dnglab's own error text surfaced in the session -- see import_worker.py's
# per-file "failed" handling -- never a crash or a silently-skipped file.
RAW_EXTENSIONS = {
    ".arw", ".srf", ".sr2",              # Sony
    ".cr2", ".cr3", ".crw", ".crm",      # Canon (crm = Cinema RAW Light)
    ".nef", ".nrw",                      # Nikon
    ".raf",                              # Fujifilm
    ".orf", ".ori",                      # Olympus
    ".rw2", ".raw", ".rwl",              # Panasonic / Leica
    ".pef",                              # Pentax / Ricoh
    ".mrw",                              # Minolta
    ".srw",                              # Samsung
    ".erf",                              # Epson
    ".kdc", ".dcs", ".dcr",              # Kodak
    ".3fr", ".fff",                      # Hasselblad
    ".mef",                              # Mamiya
    ".iiq", ".mos",                      # Phase One / Leaf
    ".ari",                              # ARRI
    ".x3f",                              # Sigma (Foveon)
    ".qtk",                              # Apple QuickTake
    ".dng",                              # DNG passthrough (iPhone ProRAW, native-DNG cameras)
}

# Video formats the same cameras/iPhone produce alongside stills. These are
# never touched by dnglab -- just renamed and filed like everything else
# (see `is_video` below), so unlike RAW_EXTENSIONS there's no decode-
# compatibility concern gating what belongs here.
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v",              # most cameras and phones, incl. iPhone
    ".mts", ".m2ts",                     # AVCHD (older Sony/Panasonic camcorders)
    ".avi",                              # legacy
}


@dataclass
class Candidate:
    path: Path
    size_bytes: int
    camera_model: str
    captured_at: str | None  # ISO 8601 local wall-clock time, as recorded by the camera
    # True when camera_model didn't come from this file's own metadata --
    # see _fill_missing_camera_models. Tells the import pipeline to write
    # the borrowed value back into the placed file (video only).
    camera_model_inferred: bool = False


def is_dng(path: Path) -> bool:
    """DNG sources (iPhone ProRAW, or a camera that shoots DNG natively)
    need no raw->DNG conversion -- the import pipeline copies these
    straight through instead of running them past dnglab."""
    return path.suffix.lower() == ".dng"


def is_video(path: Path) -> bool:
    """Videos are never converted -- copied straight through like a DNG,
    keeping their original extension, just renamed into the library
    alongside the stills from the same shoot."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _drop_deferred_duplicates(files: list[Path]) -> list[Path]:
    """When both a plain-numbered file and its trailing-letter "deferred
    processing" sibling exist (e.g. IMG_7731.DNG alongside IMG_7731D.DNG --
    confirmed live against a real iPhone, same capture second, very
    different file sizes, consistent with Apple's deferred/background photo
    processing pipeline), only the plain one should ever be imported/shown.
    A lettered file with no plain sibling present is kept as-is -- there's
    nothing to prefer it over. Siblings must share both the same parent
    directory and the same extension; same digits elsewhere don't count."""
    stems_by_dir_ext: dict[tuple[Path, str], set[str]] = {}
    for f in files:
        key = (f.parent, f.suffix.lower())
        stems_by_dir_ext.setdefault(key, set()).add(f.stem)

    kept = []
    for f in files:
        m = re.match(r"^(.*\d)[A-Za-z]+$", f.stem)
        if m:
            plain_stem = m.group(1)
            if plain_stem in stems_by_dir_ext[(f.parent, f.suffix.lower())]:
                continue  # plain sibling also present -- skip this deferred variant
        kept.append(f)
    return kept


def find_importable_files(root: Path) -> list[Path]:
    extensions = RAW_EXTENSIONS | VIDEO_EXTENSIONS
    found = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in extensions and not p.name.startswith(".")
    )
    return _drop_deferred_duplicates(found)


def read_metadata(files: list[Path], conn: sqlite3.Connection | None = None) -> dict[Path, Candidate]:
    """Per-file metadata, exiftool-batch-read for whatever isn't already
    cached. `conn`, if given, both serves and populates a persistent
    cache (metadata_cache) keyed by path alone -- confirmed live that the
    exiftool read for an iPhone's ~700-file DCIM tree took nearly 21
    seconds, the dominant cost of every single scan (picker open, source-
    card stats refresh, and an import's checking phase) even though the
    same few hundred files are unchanged scan to scan. A cache hit is
    revalidated against the file's current size (see _load_cached_metadata)
    since, unlike this app's own destination-library paths, a source mount
    path can get reused for a different physical card/device between scans.
    Passing conn=None (e.g. from a context with no DB handle) just always
    does the full exiftool read, same as before this cache existed."""
    if not files:
        return {}
    cached: dict[Path, Candidate] = {}
    to_fetch = files
    if conn is not None:
        cached = _load_cached_metadata(conn, files)
        to_fetch = [f for f in files if f not in cached]
    fresh = _read_metadata_uncached(to_fetch) if to_fetch else {}
    if conn is not None and fresh:
        _store_metadata_cache(conn, fresh)
    combined = {**cached, **fresh}
    _fill_missing_camera_models(combined)
    return combined


def _load_cached_metadata(conn: sqlite3.Connection, files: list[Path]) -> dict[Path, Candidate]:
    if not files:
        return {}
    placeholders = ",".join("?" * len(files))
    rows = conn.execute(
        f"SELECT path, size_bytes, camera_model, captured_at, camera_model_inferred "
        f"FROM metadata_cache WHERE path IN ({placeholders})",
        [str(f) for f in files],
    ).fetchall()
    by_path = {row["path"]: row for row in rows}
    out: dict[Path, Candidate] = {}
    for f in files:
        row = by_path.get(str(f))
        if row is None:
            continue
        # A cache hit is only trusted if the file's current size still
        # matches what was cached -- the cache is keyed on path alone, but
        # a *mount path* (e.g. an SD card reader's /run/media/user/EOS_DIGITAL)
        # commonly gets reused across different physical cards reformatted
        # between shoots, unlike this app's own destination-library paths,
        # which really are permanent once written. A stale hit here would
        # silently misfile a photo under another card's date/camera model.
        # Doesn't catch same-size content swaps (rare, and the same blind
        # spot quick_duplicate_match already accepts elsewhere), but a
        # stat() is cheap enough to always pay for this extra safety.
        try:
            current_size = f.stat().st_size
        except OSError:
            continue
        if current_size != row["size_bytes"]:
            continue
        out[f] = Candidate(
            path=f, size_bytes=row["size_bytes"], camera_model=row["camera_model"],
            captured_at=row["captured_at"], camera_model_inferred=bool(row["camera_model_inferred"]),
        )
    return out


def _store_metadata_cache(conn: sqlite3.Connection, candidates: dict[Path, Candidate]) -> None:
    now = datetime.now().isoformat()
    with conn:
        conn.executemany(
            "INSERT INTO metadata_cache (path, size_bytes, camera_model, captured_at, camera_model_inferred, "
            "cached_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size_bytes = excluded.size_bytes, "
            "camera_model = excluded.camera_model, captured_at = excluded.captured_at, "
            "camera_model_inferred = excluded.camera_model_inferred, cached_at = excluded.cached_at",
            [
                (str(f), c.size_bytes, c.camera_model, c.captured_at, int(c.camera_model_inferred), now)
                for f, c in candidates.items()
            ],
        )


def _read_metadata_uncached(files: list[Path]) -> dict[Path, Candidate]:
    """One exiftool invocation for the whole batch, via an argfile so a
    full card of a few hundred files stays well under ARG_MAX."""
    if not files:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as argfile:
        argfile.write("\n".join(str(f) for f in files))
        argfile_path = argfile.name
    try:
        result = subprocess.run(
            # -CreateDate is the fallback for video files, most of which
            # have no -DateTimeOriginal (an EXIF/still-photo tag) at all.
            ["exiftool", "-@", argfile_path, "-j", "-Model", "-DateTimeOriginal", "-CreateDate", "-FileSize#"],
            capture_output=True, text=True, timeout=EXIFTOOL_BATCH_TIMEOUT_S,
        )
    finally:
        Path(argfile_path).unlink(missing_ok=True)

    # exiftool exits 1 if any single file in the batch had a warning, even
    # though it still emits valid JSON for everything else -- only bail if
    # there's genuinely nothing to parse.
    if not result.stdout:
        return {}
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    out: dict[Path, Candidate] = {}
    for rec in records:
        source = rec.get("SourceFile")
        if not source:
            continue
        path = Path(source)
        size = rec.get("FileSize")
        if size is None:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
        out[path] = Candidate(
            path=path,
            size_bytes=int(size),
            camera_model=str(rec.get("Model") or "").strip(),
            captured_at=_parse_exif_datetime(rec.get("DateTimeOriginal")) or _parse_exif_datetime(rec.get("CreateDate")),
        )
    # _fill_missing_camera_models runs in the public read_metadata()
    # wrapper instead, over the *combined* cached+fresh set -- doing it
    # here too would mean inference only ever sees whichever subset of
    # files actually needed a fresh exiftool read, not the full picture.
    return out


def _fill_missing_camera_models(candidates: dict[Path, Candidate]) -> None:
    """Some cameras don't embed a Model tag in their video files the way
    they do in stills (confirmed against this machine's own library:
    videos like "2022.06.03__221935228.mp4" already show the resulting
    empty-model gap in their filename), which breaks both the library
    filename (the YYYY.MM.DD_Model_NNNNN convention loses its model
    segment) and dedup's (camera_model, filename, size) quick-match. If
    every OTHER file with a known model in this same scan agrees on
    exactly one, that's a safe stand-in -- a source root is one card/
    folder in practice (this also covers Sony's video/stills living in
    separate directory trees on the same card, since this runs over the
    whole scan, not per-directory). Deliberately doesn't touch anything
    else (date/time, lens) -- those genuinely vary per file and can't be
    inferred this way."""
    models = {c.camera_model for c in candidates.values() if c.camera_model}
    if len(models) != 1:
        return  # no siblings with a known model, or a mixed-camera batch -- don't guess
    (fallback_model,) = models
    for cand in candidates.values():
        if not cand.camera_model:
            cand.camera_model = fallback_model
            cand.camera_model_inferred = True


_EXIF_DT_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def _parse_exif_datetime(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    m = _EXIF_DT_RE.match(value)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    try:
        # Some cameras/video files write a placeholder all-zero timestamp
        # ("0000:00:00 00:00:00") when they never actually recorded one --
        # confirmed live: this crashed the whole import session with an
        # uncaught ValueError from date.fromisoformat("0000-...") three
        # steps downstream in converter.library_dest_path, silently
        # killing the worker thread and leaving the session stuck showing
        # "Filing" forever with no error surfaced anywhere. The regex
        # above only checks the *shape* (6 groups of digits), not that
        # they form a real calendar date/time -- do that here instead,
        # and fall back to None (library_dest_path already falls back to
        # the file's mtime when captured_at is None) rather than ever
        # handing back a string that merely looks like a valid date.
        datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"


def shot_number(filename: str) -> str:
    """Trailing digit run of the original filename (e.g. "DSC01075.ARW" ->
    "01075"), matching the numbering already used throughout the existing,
    Lightroom-imported library. Also allows trailing letters after the
    digits (e.g. "IMG_7731D.DNG" -> "7731D") -- confirmed live against a
    real iPhone: it names some assets with a single trailing disambiguator
    letter (evidence points to Apple's deferred/background photo
    processing pipeline -- a same-second-timestamp sibling with a wildly
    different file size, and the "D" file itself no longer present on the
    device shortly after, consistent with a transient intermediate result
    -- though Apple doesn't document the exact semantics). Without this,
    a name like "IMG_7731D" doesn't end in a bare digit run at all, so
    this fell all the way back to the *whole* stem, landing "IMG_" itself
    in the library filename ("..._IMG_7731D.dng" instead of
    "..._7731D.dng"). Falls back to the full stem for a camera that names
    files with no trailing digits at all."""
    stem = Path(filename).stem
    m = re.search(r"(\d+[A-Za-z]*)$", stem)
    return m.group(1) if m else stem


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def hash_and_stage(path: Path, dest: Path) -> str:
    """Like hash_file, but also writes every chunk read to `dest` (a local
    scratch/staging file -- never the final library placement, which is
    the caller's job). Used so a file on a slow source (an iPhone's AFC
    mount, confirmed live: video imports were taking dramatically longer
    than photo imports of a similar count) only has to be read across
    that slow connection once, whether it's then converted by dnglab or
    copied through unchanged, instead of once here to hash it and again
    later to actually place it. `dest`'s parent must already exist."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as src, open(dest, "wb") as out:
            while chunk := src.read(HASH_CHUNK_SIZE):
                h.update(chunk)
                out.write(chunk)
    except OSError:
        dest.unlink(missing_ok=True)
        raise
    return h.hexdigest()


def quick_duplicate_match(conn: sqlite3.Connection, camera_model: str, filename: str, size_bytes: int) -> str | None:
    """Cheap pre-check (no hashing) so re-scanning a mostly-already-imported
    card doesn't re-hash every file on it -- only files that don't match on
    (model, original filename, size) fall through to the authoritative
    content-hash check. Returns the existing dest_path (so callers can
    still show/open the already-imported file), or None if there's no
    match."""
    row = conn.execute(
        "SELECT dest_path FROM imports WHERE camera_model = ? AND source_filename = ? AND source_bytes = ? LIMIT 1",
        (camera_model, filename, size_bytes),
    ).fetchone()
    return row["dest_path"] if row else None


def quick_duplicate_check(conn: sqlite3.Connection, camera_model: str, filename: str, size_bytes: int) -> bool:
    return quick_duplicate_match(conn, camera_model, filename, size_bytes) is not None


def hash_duplicate_check(conn: sqlite3.Connection, source_hash: str) -> str | None:
    """Returns the existing dest_path if this exact file content was
    already imported (possibly under a different original filename)."""
    row = conn.execute("SELECT dest_path FROM imports WHERE source_hash = ?", (source_hash,)).fetchone()
    return row["dest_path"] if row else None
