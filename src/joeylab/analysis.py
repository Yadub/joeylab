"""Per-image quality signals. Runs in worker processes; reads sources, writes only thumbnails."""

from __future__ import annotations

import atexit
import errno
import os
import traceback

# Quiet MediaPipe/TFLite C++ logging (inherited by spawned workers).
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .config import ANALYSIS_SIDE, FACE_MODEL, THUMB_SIDE
from .db import thumbs_dir, to_i64
from .hashing import dhash, phash
from .imaging import DAMAGED, load_preview

MAIN_FACE_MIN_AREA = 0.004   # fraction of frame; smaller faces are "background people"
EYES_CLOSED_BLINK = 0.6    # both eyelids above this => closed (calibrated on real shots)
LAUGH_SMILE = 0.85          # big smiles squint the eyes; don't call those closed

# Bump when the analysis changes so existing catalogs re-analyse automatically.
ANALYSIS_VERSION = "3"

_face = None  # per-process FaceLandmarker


def faces_available() -> bool:
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        return False
    return FACE_MODEL.exists()


SMALL_BATCH = 4   # this few photos are analysed in-process: starting workers costs more
_inproc = {"ready": None, "lock": __import__("threading").Lock()}


def _close_inproc() -> None:
    """MediaPipe aborts the process if a landmarker is still open at interpreter exit."""
    global _face
    if _face is not None:
        try:
            _face.close()
        except Exception:
            pass
        _face = None
    _inproc["ready"] = None


def _init_worker(use_faces: bool, quiet: bool = True) -> None:
    global _face
    if quiet:
        cv2.setNumThreads(1)
        # Workers report errors as return values; silence native-library chatter on stderr.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
    if use_faces and faces_available():
        from mediapipe.tasks import python as mp_tasks
        from mediapipe.tasks.python import vision

        opts = vision.FaceLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=str(FACE_MODEL),
                                             delegate=mp_tasks.BaseOptions.Delegate.CPU),
            running_mode=vision.RunningMode.IMAGE,
            num_faces=12,
            min_face_detection_confidence=0.5,
            output_face_blendshapes=True,
        )
        _face = vision.FaceLandmarker.create_from_options(opts)
        atexit.register(_close_inproc)   # in pool workers too: MediaPipe aborts if left open at exit


def _lap_var(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def sharpness_metrics(gray: np.ndarray) -> dict:
    h, w = gray.shape
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    n = 8
    tiles = []
    for i in range(n):
        for j in range(n):
            t = lap[i * h // n : (i + 1) * h // n, j * w // n : (j + 1) * w // n]
            tiles.append(t.var())
    tiles = np.sort(np.array(tiles))[::-1]
    center = lap[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
    return {
        "sharpness": float(lap.var()),
        "sharp_center": float(center.var()),
        # Mean of the 3 sharpest tiles: the in-focus subject, even with bokeh.
        "sharp_subject": float(tiles[:3].mean()),
    }


def exposure_metrics(rgb: np.ndarray, gray: np.ndarray) -> dict:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    return {
        "brightness": float(gray.mean()),
        "contrast": float(gray.std()),
        "clip_high": float((gray >= 250).mean()),
        "clip_low": float((gray <= 5).mean()),
        "saturation": float(hsv[..., 1].mean()),
    }


def face_metrics(rgb: np.ndarray, gray: np.ndarray) -> dict:
    out = {"faces": 0, "main_faces": 0, "eyes_closed": 0, "face_sharp": None, "face_area": None, "smile": None}
    if _face is None:
        return out
    import mediapipe as mp

    res = _face.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
    h, w = gray.shape
    mains = []
    for lms, shapes in zip(res.face_landmarks, res.face_blendshapes or [[]] * len(res.face_landmarks)):
        xs = np.array([p.x for p in lms]) * w
        ys = np.array([p.y for p in lms]) * h
        x0, x1 = int(max(xs.min(), 0)), int(min(xs.max(), w))
        y0, y1 = int(max(ys.min(), 0)), int(min(ys.max(), h))
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        area = (x1 - x0) * (y1 - y0) / (w * h)
        s = {c.category_name: c.score for c in shapes}
        blink = min(s.get("eyeBlinkLeft", 0), s.get("eyeBlinkRight", 0))
        squint = min(s.get("eyeSquintLeft", 0), s.get("eyeSquintRight", 0))
        smile = (s.get("mouthSmileLeft", 0) + s.get("mouthSmileRight", 0)) / 2
        closed = blink > EYES_CLOSED_BLINK and not (smile > LAUGH_SMILE and squint > 0.5)
        crop = gray[y0:y1, x0:x1]
        if crop.shape[1] > 256:   # same scale as the faces stage: never enlarge a small face
            crop = cv2.resize(crop, (256, int(256 * crop.shape[0] / crop.shape[1])), interpolation=cv2.INTER_AREA)
        mains.append({"area": area, "closed": closed, "smile": smile, "sharp": _lap_var(crop)})
    out["faces"] = len(mains)
    mains = [m for m in mains if m["area"] >= MAIN_FACE_MIN_AREA]
    if mains:
        mains.sort(key=lambda m: -m["area"])
        out["main_faces"] = len(mains)
        out["eyes_closed"] = sum(m["closed"] for m in mains)
        out["face_area"] = mains[0]["area"]
        weights = np.array([m["area"] for m in mains])
        out["face_sharp"] = float(max(m["sharp"] for m in mains[:3]))  # mains is sorted largest first
        out["smile"] = float(np.average([m["smile"] for m in mains], weights=weights))
    return out


_PASSING = {errno.ENOENT, errno.EIO, errno.ENXIO, errno.ENODEV, errno.EBUSY, errno.ETIMEDOUT, errno.EAGAIN}


def analyze_one(task: dict) -> dict:
    """task: shot_id, path, kind, orientation, fp, thumb_path."""
    try:
        img = load_preview(task["path"], task["kind"], task["orientation"], max_side=ANALYSIS_SIDE)
        rgb = np.asarray(img)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

        thumb = img.copy()
        thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
        Path(task["thumb_path"]).parent.mkdir(parents=True, exist_ok=True)
        tmp = task["thumb_path"] + ".tmp"
        thumb.save(tmp, "JPEG", quality=85)
        os.replace(tmp, task["thumb_path"])

        m = {**sharpness_metrics(gray), **exposure_metrics(rgb, gray), **face_metrics(rgb, gray)}
        damaged = img.info.get(DAMAGED)
        return {
            "shot_id": task["shot_id"], "fp": task["fp"], "metrics": m,
            "phash": to_i64(phash(gray)), "dhash": to_i64(dhash(gray)),
            "thumb": task["thumb_path"], "error": None,
            "damaged": f"Damaged file, shown from the part that could be read ({damaged})" if damaged else None,
        }
    except Exception as e:
        # A file that vanished or couldn't be read right now (a drive dropping out) is tried again
        # next time; only a file that reads fine but can't be decoded is set aside until it changes.
        transient = (isinstance(e, OSError) and getattr(e, "errno", None) in _PASSING) or not os.path.exists(task["path"])
        return {"shot_id": task["shot_id"], "fp": task["fp"], "error": f"{type(e).__name__}: {e}",
                "transient": transient, "trace": traceback.format_exc(limit=3)}


def pending_tasks(conn, project: str, force: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.orientation, s.analyzed_fp, s.error_fp, f.path, f.kind, f.fp FROM shots s"
        " JOIN files f ON f.id = s.preview_file_id WHERE s.status='present' AND f.status='present'"
    ).fetchall()
    tdir = thumbs_dir(project)
    tasks = []
    for r in rows:
        stamp = f"{r['fp']}:{ANALYSIS_VERSION}"
        if not force and (r["analyzed_fp"] == stamp or r["error_fp"] == stamp):
            continue   # done, or failed on exactly this file: retried only once the file changes
        tasks.append({
            "shot_id": r["id"], "path": r["path"], "kind": r["kind"],
            "orientation": r["orientation"] or 1, "fp": stamp,
            "thumb_path": str(tdir / f"{r['id'] % 100:02d}" / f"{r['id']}.jpg"),
        })
    return tasks


METRIC_COLS = ["sharpness", "sharp_center", "sharp_subject", "brightness", "contrast", "clip_high",
               "clip_low", "saturation", "faces", "main_faces", "eyes_closed", "face_sharp", "face_area", "smile"]


def run(conn, project: str, force: bool = False, workers: int | None = None, use_faces: bool = True,
        progress: Callable[[int, int], None] | None = None) -> tuple[int, list[str]]:
    tasks = pending_tasks(conn, project, force)
    if not tasks:
        return 0, []
    workers = workers or max(1, (os.cpu_count() or 4) - 2)
    errors = []
    done = 0
    def results():
        if len(tasks) <= SMALL_BATCH:
            with _inproc["lock"]:
                if _inproc["ready"] != use_faces:
                    _init_worker(use_faces, quiet=False)
                    _inproc["ready"] = use_faces
                    atexit.register(_close_inproc)
                for t in tasks:
                    yield analyze_one(t)
            return
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"), initializer=_init_worker,
                                 initargs=(use_faces,)) as pool:
            for fut in as_completed([pool.submit(analyze_one, t) for t in tasks]):
                yield fut.result()

    for r in results():
        done += 1
        if r["error"]:
            errors.append(f"shot {r['shot_id']}: {r['error']}")
            conn.execute("UPDATE shots SET error=?, error_fp=? WHERE id=?",
                         (r["error"], None if r.get("transient") else r["fp"], r["shot_id"]))
        else:
            m = r["metrics"]
            conn.execute(
                f"INSERT INTO metrics(shot_id, {', '.join(METRIC_COLS)}) VALUES (?{', ?' * len(METRIC_COLS)})"
                f" ON CONFLICT(shot_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in METRIC_COLS)}",
                (r["shot_id"], *[m[c] for c in METRIC_COLS]),
            )
            conn.execute(
                "UPDATE shots SET analyzed_fp=?, thumb=?, phash=?, dhash=?, error=?, error_fp=NULL WHERE id=?",
                (r["fp"], r["thumb"], r["phash"], r["dhash"], r.get("damaged"), r["shot_id"]),
            )
        if done % 20 == 0:
            conn.commit()
        if progress:
            progress(done, len(tasks))
    conn.commit()
    return done, errors
