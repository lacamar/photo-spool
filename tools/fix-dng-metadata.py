#!/usr/bin/env python3
"""Repair metadata in DNGs converted by pre-0.8.0-6 dnglab so they match
Adobe DNG Converter output (lens profile matching in Lightroom, Make,
makernotes). Dry run unless --apply. Raw image data is never touched:
each file is rewritten to a temp copy, its image-data hash and new tags
are verified, then it atomically replaces the original."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

MAKES = {"Sony": "SONY"}

# dnglab lens-database name -> (camera's own LensModel, LensInfo)
LENSES = {
    "35-150mm F2-F2.8 Di III VXD": ("E 35-150mm F2.0-F2.8 A058", "35 150 2 2.8"),
}

INFO_TAGS = {
    "ILCE-7RM3": {
        "ExifIFD:FocalPlaneXResolution": "2164.4328",
        "ExifIFD:FocalPlaneYResolution": "2164.4328",
        "ExifIFD:FocalPlaneResolutionUnit": "3",
    },
}
DEFAULT_DNG_TAGS = {
    "IFD0:AnalogBalance": "1 1 1",
    "IFD0:LinearResponseLimit": "1",
    "IFD0:ShadowScale": "1",
    "SubIFD:AntiAliasStrength": "1",
}
RENDER_TAGS = {
    "ILCE-7RM3": {
        "IFD0:BaselineExposure": "0.15",
        "IFD0:BaselineNoise": "0.6",
        "IFD0:BaselineSharpness": "1.33",
        "SubIFD:BayerGreenSplit": "250",
        "IFD0:CameraCalibration1": "0.984 0 0 0 1 0 0 0 1",
        "IFD0:CameraCalibration2": "0.984 0 0 0 1 0 0 0 1",
        "IFD0:CameraCalibrationSig": "com.adobe",
    },
}
RAW_COPY_TAGS = [
    "IFD0:Make", "IFD0:DNGLensInfo",
    "ExifIFD:LensModel", "ExifIFD:LensInfo", "ExifIFD:ExifVersion",
    "ExifIFD:FileSource", "ExifIFD:SceneType", "ExifIFD:CustomRendered", "ExifIFD:DigitalZoomRatio",
    "ExifIFD:FocalLengthIn35mmFormat", "ExifIFD:Contrast", "ExifIFD:Saturation", "ExifIFD:Sharpness",
    "ExifIFD:ApertureValue", "ExifIFD:ShutterSpeedValue",
]
XMP_LENS_MIRRORS = {"XMP-aux:Lens": "ExifIFD:LensModel", "XMP-exifEX:LensModel": "ExifIFD:LensModel",
                    "XMP-aux:LensInfo": "ExifIFD:LensInfo"}
LENS_MAKE_TAGS = ["ExifIFD:LensMake", "XMP-exifEX:LensMake"]

READ_TAGS = sorted({
    "IFD0:Model", "IFD0:OriginalRawFileName", "IFD0:DNGAdobeData", *RAW_COPY_TAGS, *XMP_LENS_MIRRORS, *LENS_MAKE_TAGS,
    *DEFAULT_DNG_TAGS, *(t for m in (INFO_TAGS, RENDER_TAGS) for tags in m.values() for t in tags),
})

print_lock = threading.Lock()


def log(*args) -> None:
    with print_lock:
        print(*args, flush=True)


def xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def exiftool_json(paths: list[Path], tags: list[str], extra: list[str] | None = None) -> list[dict]:
    if not paths:
        return []
    out = subprocess.run(
        ["exiftool", "-json", "-n", "-G1", "-a", "-m", *(extra or []), *(f"-{t}" for t in tags), *map(str, paths)],
        capture_output=True, text=True,
    )
    return json.loads(out.stdout or "[]")


def same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    ta, tb = str(a).split(), str(b).split()
    if len(ta) != len(tb):
        return False
    for x, y in zip(ta, tb):
        try:
            if abs(float(x) - float(y)) > 1e-4 * max(1.0, abs(float(y))):
                return False
        except ValueError:
            if x != y:
                return False
    return True


def image_hashes(paths: list[Path]) -> list[str | None]:
    rows = exiftool_json(paths, ["ImageDataHash"], ["-api", "RequestTags=ImageDataHash", "-api", "ImageHashType=SHA256"])
    by_path = {r["SourceFile"]: next((v for k, v in r.items() if k.endswith("ImageDataHash")), None) for r in rows}
    return [by_path.get(str(p)) for p in paths]


class Plan:
    def __init__(self, path: Path):
        self.path = path
        self.set: dict[str, str] = {}
        self.delete: list[str] = []
        self.blob: Path | None = None
        self.before: dict[str, object] = {}

    def want(self, cur: dict, tag: str, value, only_if_missing: bool = False) -> None:
        old = cur.get(tag)
        if value is None or (only_if_missing and old is not None) or same(old, value):
            return
        self.set[tag] = str(value)
        self.before[tag] = old

    def drop(self, cur: dict, tag: str) -> None:
        if tag in cur:
            self.delete.append(tag)
            self.before[tag] = cur[tag]

    def summary(self) -> list[str]:
        items = [f"{t}={v!r}" for t, v in self.set.items()] + [f"-{t}" for t in self.delete]
        if self.blob:
            items.append("+DNGAdobeData(makernotes)")
        return items

    def empty(self) -> bool:
        return not (self.set or self.delete or self.blob)


def plan_without_raw(plan: Plan, cur: dict, source_filename: str | None, render: bool) -> None:
    make = cur.get("IFD0:Make")
    if make in MAKES:
        plan.want(cur, "IFD0:Make", MAKES[make])
    lens = LENSES.get(cur.get("ExifIFD:LensModel"))
    if lens:
        model, info = lens
        plan.want(cur, "ExifIFD:LensModel", model)
        plan.want(cur, "ExifIFD:LensInfo", info)
        plan.want(cur, "IFD0:DNGLensInfo", info)
        for xmp, src in XMP_LENS_MIRRORS.items():
            if xmp in cur:
                plan.want(cur, xmp, model if src.endswith("LensModel") else info)
        for tag in LENS_MAKE_TAGS:
            plan.drop(cur, tag)
    if source_filename:
        plan.want(cur, "IFD0:OriginalRawFileName", source_filename, only_if_missing=True)
    camera = cur.get("IFD0:Model")
    for tag, value in {**DEFAULT_DNG_TAGS, **INFO_TAGS.get(camera, {})}.items():
        plan.want(cur, tag, value, only_if_missing=True)
    if render:
        for tag, value in RENDER_TAGS.get(camera, {}).items():
            plan.want(cur, tag, value)


def plan_from_raw(plan: Plan, cur: dict, raw: Path, dnglab: str, workdir: Path, render: bool) -> None:
    out = workdir / (plan.path.stem + ".ref.dng")
    subprocess.run(
        [dnglab, "convert", "-f", "--embed-raw", "false", "--dng-preview", "false", "--dng-thumbnail", "false",
         str(raw), str(out)],
        capture_output=True, text=True, check=True,
    )
    ref = exiftool_json([out], READ_TAGS)[0]
    if "IFD0:DNGAdobeData" not in ref:
        raise RuntimeError(f"{dnglab} output has no DNGAdobeData -- dnglab lacks 0004-adobe-dng-private-data.patch")
    for tag in RAW_COPY_TAGS:
        plan.want(cur, tag, ref.get(tag))
    for xmp, src in XMP_LENS_MIRRORS.items():
        if xmp in cur:
            plan.want(cur, xmp, ref.get(src))
    for tag in LENS_MAKE_TAGS:
        if ref.get("ExifIFD:LensMake") is None:
            plan.drop(cur, tag)
    plan.want(cur, "IFD0:OriginalRawFileName", raw.name, only_if_missing=True)
    for tag in {**DEFAULT_DNG_TAGS, **INFO_TAGS.get(cur.get("IFD0:Model"), {})}:
        plan.want(cur, tag, ref.get(tag), only_if_missing=True)
    if render:
        for tag in RENDER_TAGS.get(cur.get("IFD0:Model"), {}):
            plan.want(cur, tag, ref.get(tag))
    if "IFD0:DNGAdobeData" not in cur:
        blob = workdir / (plan.path.stem + ".adobedata")
        with open(blob, "wb") as f:
            subprocess.run(["exiftool", "-m", "-b", "-DNGAdobeData", str(out)], stdout=f, check=True)
        plan.blob = blob
    out.unlink(missing_ok=True)


def apply(plan: Plan, backup_dir: Path | None) -> None:
    src = plan.path
    tmp = src.with_name(f".{src.stem}.fixtmp.dng")
    tmp.unlink(missing_ok=True)
    args = ["exiftool", "-m", "-n", "-o", str(tmp)]
    args += [f"-{t}={v}" for t, v in plan.set.items()]
    args += [f"-{t}=" for t in plan.delete]
    if plan.blob:
        args.append(f"-DNGAdobeData<={plan.blob}")
    try:
        subprocess.run([*args, str(src)], capture_output=True, text=True, check=True)
        before_hash, after_hash = image_hashes([src, tmp])
        if not before_hash or before_hash != after_hash:
            raise RuntimeError(f"image data hash mismatch ({before_hash} != {after_hash})")
        new = exiftool_json([tmp], READ_TAGS)[0]
        bad = [t for t, v in plan.set.items() if not same(new.get(t), v)]
        bad += [t for t in plan.delete if t in new]
        if plan.blob and "IFD0:DNGAdobeData" not in new:
            bad.append("IFD0:DNGAdobeData")
        if bad:
            raise RuntimeError(f"tags not written as expected: {', '.join(bad)}")
        shutil.copymode(src, tmp)
        if backup_dir:
            dest = backup_dir / src.relative_to(src.anchor)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        os.replace(tmp, src)
    finally:
        tmp.unlink(missing_ok=True)


def db_rows(db: Path) -> dict[str, sqlite3.Row]:
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT dest_path, source_filename, source_bytes, source_hash FROM imports "
        "WHERE lower(dest_path) LIKE '%.dng' AND lower(source_filename) NOT LIKE '%.dng'"
    ).fetchall()
    conn.close()
    return {r["dest_path"]: r for r in rows}


def index_raws(dirs: list[Path]) -> dict[tuple[str, int], list[Path]]:
    index: dict[tuple[str, int], list[Path]] = {}
    for d in dirs:
        for p in d.rglob("*"):
            if p.is_file():
                index.setdefault((p.name, p.stat().st_size), []).append(p)
    return index


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def find_raw(row: sqlite3.Row | None, index: dict) -> Path | None:
    if row is None:
        return None
    for p in index.get((row["source_filename"], row["source_bytes"]), []):
        if sha256(p) == row["source_hash"]:
            return p
    return None


def lightroom_running() -> bool:
    return subprocess.run(["pgrep", "-if", "lightroom"], capture_output=True).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path, help="DNG files or directories (default: every dnglab "
                    "conversion recorded in photo-spool's import database)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--raw-dir", action="append", type=Path, default=[],
                    help="directory holding original raws; matched by sha256 from the import database, "
                    "enables a full repair incl. makernotes (repeatable)")
    ap.add_argument("--render-tags", action="store_true",
                    help="also write BaselineExposure/Noise/Sharpness, BayerGreenSplit and CameraCalibration; "
                    "changes how Lightroom renders already-edited photos")
    ap.add_argument("--backup-dir", type=Path, help="copy each original here before replacing it")
    ap.add_argument("--dnglab", default="dnglab")
    ap.add_argument("--db", type=Path, default=xdg("XDG_DATA_HOME", ".local/share") / "photo-spool" / "data.db")
    ap.add_argument("--any-dng", action="store_true", help="with explicit paths, also check DNGs that aren't "
                    "dnglab conversions recorded in the import database")
    ap.add_argument("--ignore-lightroom", action="store_true", help="don't refuse to run while Lightroom is open")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("-v", "--verbose", action="store_true", help="list every planned change per file")
    args = ap.parse_args()

    rows = db_rows(args.db)
    if args.paths:
        files = []
        for p in args.paths:
            files += sorted(p.rglob("*.dng")) if p.is_dir() else [p]
    else:
        files = [Path(p) for p in rows]
    files = [f.resolve() for f in files if f.is_file() and not f.name.startswith(".")]
    if args.paths and not args.any_dng:
        skipped = [f for f in files if str(f) not in rows]
        files = [f for f in files if str(f) in rows]
        if skipped:
            print(f"Skipping {len(skipped)} DNGs not recorded as dnglab conversions in the import database "
                  "(--any-dng to include them)")
    if not files:
        print("No DNG files to check.")
        return 0
    if args.apply and not args.ignore_lightroom and lightroom_running():
        print("Lightroom appears to be running -- quit it first so it isn't writing these files.", file=sys.stderr)
        return 1

    raw_index = index_raws(args.raw_dir) if args.raw_dir else {}
    current = {}
    for i in range(0, len(files), 200):
        for r in exiftool_json(files[i:i + 200], READ_TAGS):
            current[r["SourceFile"]] = r

    workdir = Path(tempfile.mkdtemp(prefix="fix-dng-metadata-"))
    journal_path = None
    if args.apply:
        journal_dir = xdg("XDG_STATE_HOME", ".local/state") / "photo-spool" / "dng-metadata-fix"
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journal_dir / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    journal = open(journal_path, "a") if journal_path else None
    stats: Counter = Counter()
    unmapped: Counter = Counter()

    def process(path: Path) -> None:
        cur = current.get(str(path), {})
        row = rows.get(str(path))
        plan = Plan(path)
        try:
            raw = find_raw(row, raw_index) if raw_index else None
            if raw:
                plan_from_raw(plan, cur, raw, args.dnglab, workdir, args.render_tags)
                stats["from raw"] += 1
            else:
                plan_without_raw(plan, cur, row["source_filename"] if row else None, args.render_tags)
                if cur.get("ExifIFD:LensMake") and cur.get("ExifIFD:LensModel") not in LENSES:
                    unmapped[cur.get("ExifIFD:LensModel")] += 1
            if plan.empty():
                stats["already ok"] += 1
                return
            for tag in plan.set:
                stats[f"set {tag}"] += 1
            for tag in plan.delete:
                stats[f"delete {tag}"] += 1
            if plan.blob:
                stats["add makernotes"] += 1
            if args.verbose or args.apply:
                log(f"{'fix ' if args.apply else 'plan'} {path}: {'; '.join(plan.summary())}")
            if args.apply:
                apply(plan, args.backup_dir)
                with print_lock:
                    journal.write(json.dumps({"path": str(path), "before": plan.before, "set": plan.set,
                                              "delete": plan.delete, "makernotes": bool(plan.blob)}) + "\n")
                    journal.flush()
                stats["fixed"] += 1
            else:
                stats["would fix"] += 1
        except Exception as exc:
            stats["errors"] += 1
            log(f"ERROR {path}: {exc}")
        finally:
            if plan.blob:
                plan.blob.unlink(missing_ok=True)

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            list(pool.map(process, files))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if journal:
            journal.close()

    print(f"\n{len(files)} files checked")
    for key, n in sorted(stats.items()):
        print(f"  {key}: {n}")
    if unmapped:
        print("\nLens names substituted by dnglab with no mapping in LENSES (add the camera's own LensModel "
              "string, or use --raw-dir):")
        for name, n in unmapped.most_common():
            print(f"  {n:6d}  {name}")
    if journal_path:
        print(f"\nJournal of previous values: {journal_path}")
    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply.")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
