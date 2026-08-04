"""Runs dnglab over a staged batch of ARW files and computes/claims the
final library path for each result.

dnglab's directory mode preserves each input file's own name (just
swapping the extension), so a batch is staged through a scratch input
directory of symlinks named by content hash -- guaranteeing no clash even
between files with the same original name from different cards -- then
matched back up and renamed into place using the metadata the scanner
already extracted.
"""
from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path

from . import scanner

CONVERT_TIMEOUT_S = 60 * 30
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._+-]")


def convert_batch(dnglab_path: Path, staging_in: Path, staging_out: Path,
                   compression: str, embed_raw: bool) -> subprocess.CompletedProcess:
    staging_out.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            str(dnglab_path), "convert",
            "-c", compression,
            "--embed-raw", "true" if embed_raw else "false",
            "--keep-mtime", "true",
            "-r", "-f",
            str(staging_in), str(staging_out),
        ],
        capture_output=True, text=True, timeout=CONVERT_TIMEOUT_S,
    )


def library_dest_path(library_root: Path, candidate: "scanner.Candidate") -> Path:
    if candidate.captured_at:
        d = date.fromisoformat(candidate.captured_at[:10])
    else:
        d = date.fromtimestamp(candidate.path.stat().st_mtime)
    model = _sanitize(candidate.camera_model)
    seq = scanner.shot_number(candidate.path.name)
    filename = f"{d.strftime('%Y.%m.%d')}_{model}_{seq}.dng"
    return library_root / f"{d.year}" / d.strftime("%Y-%m") / d.strftime("%Y-%m-%d") / filename


def unique_dest_path(path: Path) -> Path:
    """If `path` is already taken, append " (n)" like a file manager would.
    Genuine re-imports of the same shot are already filtered out upstream
    by content-hash dedup, so a collision here means two *different* photos
    landed on the same computed name (e.g. the camera's shot counter
    rolled over) -- never silently overwrite in that case."""
    if not path.exists():
        return path
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def _sanitize(text: str) -> str:
    return _UNSAFE_CHARS_RE.sub("", text)
