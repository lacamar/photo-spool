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


def find_importable_files(root: Path) -> list[Path]:
    extensions = RAW_EXTENSIONS | VIDEO_EXTENSIONS
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in extensions and not p.name.startswith(".")
    )


def read_metadata(files: list[Path]) -> dict[Path, Candidate]:
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
    return out


_EXIF_DT_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def _parse_exif_datetime(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    m = _EXIF_DT_RE.match(value)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def shot_number(filename: str) -> str:
    """Trailing digit run of the original filename (e.g. "DSC01075.ARW" ->
    "01075"), matching the numbering already used throughout the existing,
    Lightroom-imported library. Falls back to the full stem for a camera
    that names files with no trailing digits."""
    stem = Path(filename).stem
    m = re.search(r"(\d+)$", stem)
    return m.group(1) if m else stem


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK_SIZE):
            h.update(chunk)
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
