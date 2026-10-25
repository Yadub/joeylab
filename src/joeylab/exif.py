"""Batch metadata reads through exiftool (read-only)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

TAGS = [
    "SubSecDateTimeOriginal", "DateTimeOriginal", "CreateDate", "MediaCreateDate", "Make", "Model",
    "SerialNumber", "InternalSerialNumber", "LensModel", "Lens", "ISO", "FNumber",
    "ExposureTime", "FocalLength", "ImageWidth", "ImageHeight", "ExifImageWidth",
    "ExifImageHeight", "Orientation",
    # time zone, place, ratings and provenance
    "OffsetTimeOriginal", "OffsetTime", "GPSLatitude", "GPSLatitudeRef", "GPSLongitude", "GPSLongitudeRef",
    "GPSAltitude", "GPSAltitudeRef", "Rating", "Label", "Software", "Duration", "FileType",
]
# Read from .xmp sidecars written by Lightroom, Capture One, darktable or JoeyLab.
SIDECAR_TAGS = ["Rating", "Label", "Subject", "HierarchicalSubject", "PersonInImage", "HasSettings", "ProcessVersion", "HistorySoftwareAgent",
                "Description"]

_DT_RE = re.compile(r"(\d{4}):(\d\d):(\d\d)[ T](\d\d):(\d\d):(\d\d)(\.\d+)?")
_OFF_RE = re.compile(r"([+-])(\d\d):?(\d\d)")
EDITORS = ("lightroom", "capture one", "photoshop", "darktable", "luminar", "affinity", "dxo", "on1", "snapseed",
           "pixelmator", "rawtherapee", "lightcraft", "photos ")


# Where Homebrew (Apple Silicon, Intel) and MacPorts put it: a launchd service starts with a bare PATH.
SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")
MISSING = ("exiftool isn't installed, so photos will be undated, with no camera, lens or GPS details. Install it ("
           + ("brew install exiftool" if sys.platform == "darwin" else "for example: sudo apt install libimage-exiftool-perl")
           + ") and update again: the details fill in.")
_found: str | None = None


def exiftool() -> str | None:
    """The absolute path of exiftool, or None. Looked up on PATH, then in SEARCH_DIRS."""
    global _found
    if _found and os.access(_found, os.X_OK):
        return _found
    path = shutil.which("exiftool")
    if not path:
        path = next((c for d in SEARCH_DIRS if os.access(c := os.path.join(d, "exiftool"), os.X_OK)), None)
    _found = path and os.path.abspath(path)
    return _found


def run_env(tool: str) -> dict:
    """exiftool is a Perl script: its folder and the system's on PATH, and a plain locale."""
    return dict(os.environ, LC_ALL="C", LANG="C",
                PATH=os.pathsep.join([os.path.dirname(tool), "/usr/bin", "/bin", os.environ.get("PATH", "")]))


def available() -> bool:
    return exiftool() is not None


def _readable(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return bool(fh.read(1))
    except OSError:
        return False


def read_many(paths: list[str], batch: int = 300, tags: list[str] | None = None,
              fast: bool = True) -> tuple[dict[str, dict], set[str]]:
    """Return ({path: tags}, paths read). A path is "read" when exiftool ran on its batch, even
    if the file had no metadata; paths in a failed batch can be retried later. fast=False for
    videos: their metadata often sits after the media data, where -fast2 stops reading."""
    out: dict[str, dict] = {}
    done: set[str] = set()
    tool = exiftool()
    if not paths or not tool:
        return out, done
    env = run_env(tool)
    # QuickTimeUTC: video dates are stored in UTC; read them as local time like photo dates.
    args = ["-json", "-n", *(["-fast2"] if fast else []), "-q", "-q", "-api", "QuickTimeUTC=1"] + [f"-{t}" for t in (tags or TAGS)]
    for i in range(0, len(paths), batch):
        chunk = paths[i : i + batch]
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
            f.write("\n".join(chunk))
            argfile = f.name
        try:
            proc = subprocess.run(
                [tool, "-charset", "filename=utf8", *args, "-@", argfile],
                capture_output=True, text=True, env=env,
            )
            if proc.stdout.strip():
                for rec in json.loads(proc.stdout):
                    out[rec.get("SourceFile", "")] = rec
            # Read means exiftool returned it, or it's plainly there and readable (a format exiftool
            # doesn't know). A file that vanished or failed to read (a drive dropping out) is tried
            # again next time instead of being stored as "no metadata" for good.
            for pth in chunk:
                if pth in out or (proc.returncode in (0, 1) and _readable(pth)):
                    done.add(pth)
        except (json.JSONDecodeError, OSError):
            pass
        finally:
            os.unlink(argfile)
    return out, done


def parse_capture(tags: dict) -> tuple[float | None, str | None]:
    """Capture time as a naive-local epoch float (the camera's clock)."""
    for key in ("SubSecDateTimeOriginal", "DateTimeOriginal", "CreateDate", "MediaCreateDate"):
        val = tags.get(key)
        if not isinstance(val, str):
            continue
        m = _DT_RE.search(val)
        if not m:
            continue
        y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
        if y < 1990:
            continue
        try:
            ts = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if m.group(7):
            ts += float(m.group(7))
        return ts, f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
    return None, None


def parse_offset(tags: dict) -> int | None:
    """Minutes east of UTC from OffsetTimeOriginal ("+05:30"), when the camera recorded it."""
    for key in ("OffsetTimeOriginal", "OffsetTime"):
        v = tags.get(key)
        if isinstance(v, str):
            m = _OFF_RE.search(v)
            if m:
                mins = int(m.group(2)) * 60 + int(m.group(3))
                return mins if m.group(1) == "+" else -mins
    return None


def parse_gps(tags: dict) -> tuple[float | None, float | None, float | None]:
    lat, lon = tags.get("GPSLatitude"), tags.get("GPSLongitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None, None, None
    if str(tags.get("GPSLatitudeRef", "N")).upper().startswith("S") and lat > 0:
        lat = -lat
    if str(tags.get("GPSLongitudeRef", "E")).upper().startswith("W") and lon > 0:
        lon = -lon
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:      # 0,0: an unset GPS block
        return None, None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None, None
    alt = tags.get("GPSAltitude")
    if isinstance(alt, (int, float)) and str(tags.get("GPSAltitudeRef", "0")) in ("1", "Below Sea Level"):
        alt = -alt
    return float(lat), float(lon), float(alt) if isinstance(alt, (int, float)) else None


def is_editor(software: str | None) -> bool:
    s = (software or "").lower()
    return any(e in s for e in EDITORS)


def summarize(tags: dict) -> dict:
    ts, cstr = parse_capture(tags)
    make = str(tags.get("Make") or "").strip()
    model = str(tags.get("Model") or "").strip()
    camera = model if (not make or model.lower().startswith(make.split()[0].lower())) else f"{make} {model}"
    serial = tags.get("SerialNumber") or tags.get("InternalSerialNumber")

    def num(k):
        v = tags.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    width = tags.get("ExifImageWidth") or tags.get("ImageWidth")
    height = tags.get("ExifImageHeight") or tags.get("ImageHeight")
    orient = tags.get("Orientation")
    lat, lon, alt = parse_gps(tags)
    rating = tags.get("Rating")
    return {
        "capture_ts": ts,
        "capture_str": cstr,
        "camera": camera or None,
        "body_serial": str(serial) if serial else None,
        "lens": (tags.get("LensModel") or tags.get("Lens") or None),
        "iso": num("ISO"),
        "aperture": num("FNumber"),
        "shutter": num("ExposureTime"),
        "focal": num("FocalLength"),
        "width": int(width) if isinstance(width, (int, float)) else None,
        "height": int(height) if isinstance(height, (int, float)) else None,
        "orientation": int(orient) if isinstance(orient, (int, float)) and 1 <= orient <= 8 else 1,
        "tz_offset": parse_offset(tags),
        "gps_lat": lat, "gps_lon": lon, "gps_alt": alt,
        "rating": int(rating) if isinstance(rating, (int, float)) and -1 <= rating <= 5 else None,
        "label": str(tags["Label"]) if tags.get("Label") else None,
        "software": str(tags["Software"]) if tags.get("Software") else None,
        "duration": num("Duration"),
        "filetype": str(tags["FileType"]) if tags.get("FileType") else None,
    }


# Extensions each detected file type may honestly carry.
_TYPE_EXTS = {"JPEG": {".jpg", ".jpeg"}, "HEIC": {".heic", ".heif", ".hif"}, "HEIF": {".heic", ".heif", ".hif"},
              "PNG": {".png"}, "TIFF": {".tif", ".tiff"}, "WEBP": {".webp"}, "MOV": {".mov"}, "MP4": {".mp4", ".m4v"},
              "DNG": {".dng"}}


def wrong_extension(filetype: str | None, ext: str) -> bool:
    """A file whose contents aren't what its name says (a PNG named .jpg, a HEIC named .jpg...)."""
    if not filetype:
        return False
    ft = filetype.upper()
    ok = _TYPE_EXTS.get(ft)
    if ok is None:
        return False             # RAW formats have many names; don't guess
    return ext.lower() not in ok


def summarize_sidecar(tags: dict) -> dict:
    """What another app recorded in a .xmp sidecar: stars, color label, keywords, people, edits."""
    def as_list(v):
        if v is None:
            return []
        return [str(x) for x in v] if isinstance(v, list) else [str(v)]

    rating = tags.get("Rating")
    agent = " ".join(as_list(tags.get("HistorySoftwareAgent")))
    return {
        "rating": int(rating) if isinstance(rating, (int, float)) and -1 <= rating <= 5 else None,
        "label": str(tags["Label"]) if tags.get("Label") else None,
        "keywords": as_list(tags.get("Subject")),
        "hierarchy": as_list(tags.get("HierarchicalSubject")),       # Lightroom's "Activities|Climbing"
        "people": as_list(tags.get("PersonInImage")),
        "edited": bool(tags.get("HasSettings") in (True, "True", 1) or tags.get("ProcessVersion") or is_editor(agent)),
        "description": str(tags["Description"]) if tags.get("Description") else None,
    }


def sidecars_for(path: str) -> list[str]:
    """Existing sidecars of a file: IMG_1.xmp (Lightroom, Capture One) and IMG_1.CR3.xmp (darktable)."""
    out = []
    for cand in (os.path.splitext(path)[0] + ".xmp", os.path.splitext(path)[0] + ".XMP", path + ".xmp"):
        if cand not in out and os.path.isfile(cand):
            out.append(cand)
    return out


C2PA_MARKERS = (b"c2pa", b"jumb")


def sniff_c2pa(path: str, head: int = 256 * 1024) -> bool:
    """Content Credentials live in a JUMBF box labelled "c2pa" near the start of the file."""
    try:
        with open(path, "rb") as f:
            data = f.read(head)
    except OSError:
        return False
    return all(m in data for m in C2PA_MARKERS)
