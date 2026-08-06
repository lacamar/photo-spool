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

import logging
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Callable

from . import scanner

logger = logging.getLogger(__name__)

CONVERT_TIMEOUT_S = 60 * 30
DNG_VERSION_TIMEOUT_S = 30
DNG_BACKWARD_VERSION = "1.4.0.0"
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._+-]")
_CONVERTED_RE = re.compile(r"Status: Converted '([^']+)' =>")


def convert_batch(dnglab_path: Path, staging_in: Path, staging_out: Path, compression: str, embed_raw: bool,
                   on_progress: Callable[[str], None] | None = None) -> tuple[int, str]:
    """Runs dnglab over the whole staged batch, streaming its `-v` per-file
    output so `on_progress(source_stem)` can be called as each file
    finishes. dnglab itself is fast (well under a second per file in
    practice); this matters because without incremental feedback here, a
    batch of a few hundred files reports nothing for however long the
    whole batch takes and reads as hung. Returns (returncode, output tail)
    for error reporting -- never raises on a timeout, just kills and
    reports it like any other failure."""
    staging_out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(dnglab_path), "convert",
            "-c", compression,
            "--embed-raw", "true" if embed_raw else "false",
            "--keep-mtime", "true",
            "-r", "-f", "-v",
            str(staging_in), str(staging_out),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail.append(line)
        if len(tail) > 50:
            tail.pop(0)
        match = _CONVERTED_RE.search(line)
        if match and on_progress is not None:
            on_progress(Path(match.group(1)).stem)
    try:
        proc.wait(timeout=CONVERT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        tail.append("Conversion timed out\n")
    return proc.returncode, "".join(tail)


def set_dng_backward_version(path: Path) -> None:
    """Rewrites DNGBackwardVersion to 1.4.0.0 in place, for every DNG that
    lands in the library -- both dnglab's own conversions and DNG-
    passthrough files (iPhone ProRAW, cameras that shoot DNG natively),
    which can arrive tagged with a newer DNG spec version than some tools
    understand. Same fix as ~/.local/bin/dng-version-converter, applied at
    import time instead of as a separate manual pass. Non-fatal on
    failure -- the file is already correctly placed either way, so a
    exiftool hiccup here shouldn't fail the whole import."""
    try:
        subprocess.run(
            ["exiftool", f"-DNGBackwardVersion={DNG_BACKWARD_VERSION}", "-overwrite_original", "-P", str(path)],
            capture_output=True, text=True, timeout=DNG_VERSION_TIMEOUT_S, check=True,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        logger.warning("Could not set DNGBackwardVersion on %s", path, exc_info=True)


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
