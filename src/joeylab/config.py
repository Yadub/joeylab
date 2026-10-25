"""Paths, file-type tables and tunable thresholds."""

from __future__ import annotations

import os
import re
from pathlib import Path

HOME = Path(os.environ.get("JOEYLAB_HOME", "~/.joeylab")).expanduser()
MODELS_DIR = HOME / "models"
FACE_MODEL = MODELS_DIR / "face_landmarker.task"
FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)
FACE_MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"

RAW_EXTS = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
    ".orf", ".rw2", ".pef", ".dng", ".srw", ".x3f", ".3fr", ".iiq", ".erf",
    ".mef", ".mos", ".kdc", ".rwl",
}
JPEG_EXTS = {".jpg", ".jpeg"}
OTHER_EXTS = {".heic", ".heif", ".hif", ".png", ".tif", ".tiff", ".webp"}   # .hif: Fujifilm HEIF
IMAGE_EXTS = RAW_EXTS | JPEG_EXTS | OTHER_EXTS
# Videos are catalogued (poster frame, duration, duplicates, timeline); an iPhone Live Photo's
# .MOV joins its still as one photo.
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".mkv"}

# Files modified this recently are assumed to still be copying and are skipped.
SETTLE_SECONDS = 5.0

# Analysis works on a downscaled copy; thumbnails are stored for the UI.
ANALYSIS_SIDE = 1600
THUMB_SIDE = 768

# Time limits for work that can hang (deadline.py): one photo in Apple Vision, one batch on the
# graphics processor. Past them, that step stops for this update and picks up in the next one.
VISION_TIMEOUT_S = 60.0
GPU_TIMEOUT_S = 180.0

# Grouping: consecutive frames from the same body are linked when they are
# close in time and look alike (perceptual-hash Hamming distance, 0-64).
BURST_GAP_S = 2.0
BURST_MAX_HAM = 30
SIMILAR_GAP_S = 45.0
SIMILAR_MAX_HAM = 20
# With CLIP embeddings (ai extra): semantic "same scene" links.
SCENE_GAP_S = 60.0
SCENE_MIN_COS = 0.90
SCENE_LONG_GAP_S = 300.0
SCENE_LONG_MIN_COS = 0.95
# How strictly frames are stacked, chosen with the grouping slider (1 = bursts only ... 5 = whole
# scenes). Level 3 is the thresholds above. A cos of 2 switches that kind of link off.
GROUPING_DEFAULT = 3
SHOOT_GROUPING = 2      # a library set up as a shoot stacks "Tight": bursts and near-identical frames
GROUPING_NAMES = {1: "Bursts only", 2: "Tight", 3: "Balanced", 4: "Loose", 5: "Whole scenes"}
GROUPING_LEVELS = {
    1: dict(burst_gap=1.0, burst_ham=24, similar_gap=0.0, similar_ham=0, scene_gap=0.0, scene_cos=2.0, long_gap=0.0, long_cos=2.0),
    2: dict(burst_gap=1.5, burst_ham=28, similar_gap=20.0, similar_ham=16, scene_gap=30.0, scene_cos=0.93, long_gap=0.0, long_cos=2.0),
    3: dict(burst_gap=BURST_GAP_S, burst_ham=BURST_MAX_HAM, similar_gap=SIMILAR_GAP_S, similar_ham=SIMILAR_MAX_HAM,
            scene_gap=SCENE_GAP_S, scene_cos=SCENE_MIN_COS, long_gap=SCENE_LONG_GAP_S, long_cos=SCENE_LONG_MIN_COS),
    4: dict(burst_gap=3.0, burst_ham=32, similar_gap=90.0, similar_ham=24, scene_gap=120.0, scene_cos=0.87, long_gap=600.0, long_cos=0.93),
    5: dict(burst_gap=5.0, burst_ham=34, similar_gap=180.0, similar_ham=28, scene_gap=300.0, scene_cos=0.84, long_gap=1800.0, long_cos=0.91),
}

# Near-duplicate copies: same capture instant and near-identical content.
DUP_MAX_HAM = 8
DUP_NOEXIF_MAX_HAM = 4

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def kind_for(ext: str) -> str | None:
    ext = ext.lower()
    if ext in RAW_EXTS:
        return "raw"
    if ext in JPEG_EXTS:
        return "jpeg"
    if ext in OTHER_EXTS:
        return "other"
    if ext in VIDEO_EXTS:
        return "video"
    return None


# Photos smaller than this on the long side are thumbnails, previews or recovered fragments.
TINY_SIDE = 640


def project_dir(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise ValueError(f"{name!r} can't be a library name: use letters, digits, - _ and . (no spaces), starting with a letter or digit")
    return HOME / "projects" / name


def private(path) -> None:
    """Only this account may read or change one of JoeyLab's own files (a catalog, a backup, a log)."""
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


_SECURED: set[str] = set()
# What's tightened in a folder made before JoeyLab kept it private: whatever holds the most.
PRIVATE_FILES = ("projects/*/catalog.db*", "backups/**/*", "logs/*", "trash-log/*", "xmp-backups/**/*", "taste.*", "copies.db*")


def secure_home() -> None:
    """Keep JoeyLab's folder (~/.joeylab) to this account alone, so other accounts on the Mac
    can't read the catalogs, previews, backups or logs (folder names, places, faces). Run once per
    process at start-up: an existing folder is tightened, with the files in it that hold the most.
    Files JoeyLab writes for you elsewhere (exports, galleries, sidecars) keep their usual
    permissions."""
    if str(HOME) in _SECURED:
        return
    try:
        HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        if HOME.stat().st_mode & 0o077:
            HOME.chmod(0o700)
    except OSError:
        return
    _SECURED.add(str(HOME))
    for pattern in PRIVATE_FILES:
        for f in HOME.glob(pattern):
            if f.is_file() and not f.is_symlink():
                private(f)


def list_projects() -> list[str]:
    base = HOME / "projects"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if (p / "catalog.db").exists())


def offline() -> bool:
    """JOEYLAB_OFFLINE=1: download nothing (models, the town list). Features whose models aren't
    on this Mac yet are skipped. The tests and CI run this way."""
    return bool(os.environ.get("JOEYLAB_OFFLINE"))


class ModelMissing(RuntimeError):
    """A model isn't on this Mac yet and downloads are off (JOEYLAB_OFFLINE)."""


def offline_first(load):
    """Run a model load from the local Hugging Face cache without asking the Hub for updates (a
    request on every start otherwise). Only when the files aren't cached yet does it go online,
    and never with JOEYLAB_OFFLINE (it raises ModelMissing instead)."""
    try:
        from huggingface_hub import constants
    except ImportError:
        if offline():
            raise ModelMissing("The model isn't downloaded and downloads are off (JOEYLAB_OFFLINE)")
        return load()
    import logging

    class CacheMiss(logging.Filter):        # open_clip logs an error when the cache-only try misses
        def filter(self, record):
            return "Failed to download" not in record.getMessage()

    was = constants.HF_HUB_OFFLINE
    constants.HF_HUB_OFFLINE = True
    quiet = CacheMiss()
    logging.getLogger().addFilter(quiet)
    try:
        return load()
    except Exception as e:
        if offline():
            raise ModelMissing("The model isn't downloaded and downloads are off (JOEYLAB_OFFLINE)") from e
        logging.getLogger().removeFilter(quiet)
        constants.HF_HUB_OFFLINE = was
        return load()
    finally:
        logging.getLogger().removeFilter(quiet)
        constants.HF_HUB_OFFLINE = was
